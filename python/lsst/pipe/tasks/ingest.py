# This file is part of pipe_tasks.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import shutil
import tempfile
import sqlite3
from fnmatch import fnmatch
from glob import glob
from contextlib import contextmanager

from lsst.pex.config import Config, Field, DictField, ListField, ConfigurableField
import lsst.pex.exceptions
from lsst.afw.fits import readMetadata
from lsst.pipe.base import Task, InputOnlyArgumentParser
from lsst.afw.fits import DEFAULT_HDU


class IngestArgumentParser(InputOnlyArgumentParser):
    """Argument parser to support ingesting images into the image repository"""

    def __init__(self, *args, **kwargs):
        super(IngestArgumentParser, self).__init__(*args, **kwargs)
        self.add_argument("-n", "--dry-run", dest="dryrun", action="store_true", default=False,
                          help="Don't perform any action?")
        self.add_argument("--mode", choices=["move", "copy", "link", "skip"], default="link",
                          help="Mode of delivering the files to their destination")
        self.add_argument("--create", action="store_true", help="Create new registry (clobber old)?")
        self.add_argument("--ignore-ingested", dest="ignoreIngested", action="store_true",
                          help="Don't register files that have already been registered")
        self.add_id_argument("--badId", "raw", "Data identifier for bad data", doMakeDataRefList=False)
        self.add_argument("--badFile", nargs="*", default=[],
                          help="Names of bad files (no path; wildcards allowed)")
        self.add_argument("files", nargs="+", help="Names of file")


class ParseConfig(Config):
    """Configuration for ParseTask"""
    translation = DictField(keytype=str, itemtype=str, default={},
                            doc="Translation table for property --> header")
    translators = DictField(keytype=str, itemtype=str, default={},
                            doc="Properties and name of translator method")
    defaults = DictField(keytype=str, itemtype=str, default={},
                         doc="Default values if header is not present")
    hdu = Field(dtype=int, default=DEFAULT_HDU, doc="HDU to read for metadata")
    extnames = ListField(dtype=str, default=[], doc="Extension names to search for")


class ParseTask(Task):
    """Task that will parse the filename and/or its contents to get the required information
    for putting the file in the correct location and populating the registry."""
    ConfigClass = ParseConfig

    def getInfo(self, filename):
        """Get information about the image from the filename and its contents

        Here, we open the image and parse the header, but one could also look at the filename itself
        and derive information from that, or set values from the configuration.

        @param filename    Name of file to inspect
        @return File properties; list of file properties for each extension
        """
        md = readMetadata(filename, self.config.hdu)
        phuInfo = self.getInfoFromMetadata(md)
        if len(self.config.extnames) == 0:
            # No extensions to worry about
            return phuInfo, [phuInfo]
        # Look in the provided extensions
        extnames = set(self.config.extnames)
        extnum = 0
        infoList = []
        while len(extnames) > 0:
            extnum += 1
            try:
                md = readMetadata(filename, extnum)
            except Exception as e:
                self.log.warn("Error reading %s extensions %s: %s" % (filename, extnames, e))
                break
            ext = self.getExtensionName(md)
            if ext in extnames:
                hduInfo = self.getInfoFromMetadata(md, info=phuInfo.copy())
                # We need the HDU number when registering MEF files.
                hduInfo["hdu"] = extnum
                infoList.append(hduInfo)
                extnames.discard(ext)
        return phuInfo, infoList

    @staticmethod
    def getExtensionName(md):
        """ Get the name of an extension.
        @param md: PropertySet like one obtained from lsst.afw.fits.readMetadata)
        @return Name of the extension if it exists.  None otherwise.
        """
        try:
            # This returns a tuple
            ext = md.getScalar("EXTNAME")
            return ext[1]
        except lsst.pex.exceptions.Exception:
            return None

    def getInfoFromMetadata(self, md, info=None):
        """Attempt to pull the desired information out of the header

        This is done through two mechanisms:
        * translation: a property is set directly from the relevant header keyword
        * translator: a property is set with the result of calling a method

        The translator methods receive the header metadata and should return the
        appropriate value, or None if the value cannot be determined.

        @param md      FITS header
        @param info    File properties, to be supplemented
        @return info
        """
        if info is None:
            info = {}
        for p, h in self.config.translation.items():
            if md.exists(h):
                value = md.getScalar(h)
                if isinstance(value, str):
                    value = value.strip()
                info[p] = value
            elif p in self.config.defaults:
                info[p] = self.config.defaults[p]
            else:
                self.log.warn("Unable to find value for %s (derived from %s)" % (p, h))
        for p, t in self.config.translators.items():
            func = getattr(self, t)
            try:
                value = func(md)
            except Exception as e:
                self.log.warn("%s failed to translate %s: %s", t, p, e)
                value = None
            if value is not None:
                info[p] = value
        return info

    def translate_date(self, md):
        """Convert a full DATE-OBS to a mere date

        Besides being an example of a translator, this is also generally useful.
        It will only be used if listed as a translator in the configuration.
        """
        date = md.getScalar("DATE-OBS").strip()
        c = date.find("T")
        if c > 0:
            date = date[:c]
        return date

    def translate_filter(self, md):
        """Translate a full filter description into a mere filter name

        Besides being an example of a translator, this is also generally useful.
        It will only be used if listed as a translator in the configuration.
        """
        filterName = md.getScalar("FILTER").strip()
        filterName = filterName.strip()
        c = filterName.find(" ")
        if c > 0:
            filterName = filterName[:c]
        return filterName

    def getDestination(self, butler, info, filename):
        """Get destination for the file

        @param butler      Data butler
        @param info        File properties, used as dataId for the butler
        @param filename    Input filename
        @return Destination filename
        """
        raw = butler.get("raw_filename", info)[0]
        # Ensure filename is devoid of cfitsio directions about HDUs
        c = raw.find("[")
        if c > 0:
            raw = raw[:c]
        return raw


class RegisterConfig(Config):
    """Configuration for the RegisterTask"""
    table = Field(dtype=str, default="raw", doc="Name of table")
    columns = DictField(keytype=str, itemtype=str, doc="List of columns for raw table, with their types",
                        itemCheck=lambda x: x in ("text", "int", "double"),
                        default={'object': 'text',
                                 'visit': 'int',
                                 'ccd': 'int',
                                 'filter': 'text',
                                 'date': 'text',
                                 'taiObs': 'text',
                                 'expTime': 'double',
                                 },
                        )
    unique = ListField(dtype=str, doc="List of columns to be declared unique for the table",
                       default=["visit", "ccd"])
    visit = ListField(dtype=str, default=["visit", "object", "date", "filter"],
                      doc="List of columns for raw_visit table")
    ignore = Field(dtype=bool, default=False, doc="Ignore duplicates in the table?")
    permissions = Field(dtype=int, default=0o664, doc="Permissions mode for registry; 0o664 = rw-rw-r--")


class RegistryContext:
    """Context manager to provide a registry

    An existing registry is copied, so that it may continue
    to be used while we add to this new registry.  Finally,
    the new registry is moved into the right place.
    """

    def __init__(self, registryName, createTableFunc, forceCreateTables, permissions):
        """Construct a context manager

        @param registryName: Name of registry file
        @param createTableFunc: Function to create tables
        @param forceCreateTables: Force the (re-)creation of tables?
        @param permissions: Permissions to set on database file
        """
        self.registryName = registryName
        self.permissions = permissions

        updateFile = tempfile.NamedTemporaryFile(prefix=registryName, dir=os.path.dirname(self.registryName),
                                                 delete=False)
        self.updateName = updateFile.name

        if os.path.exists(registryName):
            assertCanCopy(registryName, self.updateName)
            os.chmod(self.updateName, os.stat(registryName).st_mode)
            shutil.copyfile(registryName, self.updateName)

        self.conn = sqlite3.connect(self.updateName)
        createTableFunc(self.conn, forceCreateTables=forceCreateTables)
        os.chmod(self.updateName, self.permissions)

    def __enter__(self):
        """Provide the 'as' value"""
        return self.conn

    def __exit__(self, excType, excValue, traceback):
        self.conn.commit()
        self.conn.close()
        if excType is None:
            assertCanCopy(self.updateName, self.registryName)
            if os.path.exists(self.registryName):
                os.unlink(self.registryName)
            os.rename(self.updateName, self.registryName)
            os.chmod(self.registryName, self.permissions)
        return False  # Don't suppress any exceptions


@contextmanager
def fakeContext():
    """A context manager that doesn't provide any context

    Useful for dry runs where we don't want to actually do anything real.
    """
    yield


class RegisterTask(Task):
    """Task that will generate the registry for the Mapper"""
    ConfigClass = RegisterConfig
    placeHolder = '?'  # Placeholder for parameter substitution; this value suitable for sqlite3
    typemap = {'text': str, 'int': int, 'double': float}  # Mapping database type --> python type

    def openRegistry(self, directory, create=False, dryrun=False, name="registry.sqlite3"):
        """Open the registry and return the connection handle.

        @param directory  Directory in which the registry file will be placed
        @param create  Clobber any existing registry and create a new one?
        @param dryrun  Don't do anything permanent?
        @param name    Filename of the registry
        @return Database connection
        """
        if dryrun:
            return fakeContext()

        registryName = os.path.join(directory, name)
        context = RegistryContext(registryName, self.createTable, create, self.config.permissions)
        return context

    def createTable(self, conn, table=None, forceCreateTables=False):
        """Create the registry tables

        One table (typically 'raw') contains information on all files, and the
        other (typically 'raw_visit') contains information on all visits.

        @param conn    Database connection
        @param table   Name of table to create in database
        """
        cursor = conn.cursor()
        if table is None:
            table = self.config.table
        cmd = "SELECT name FROM sqlite_master WHERE type='table' AND name='%s'" % table
        cursor.execute(cmd)
        if cursor.fetchone() and not forceCreateTables:  # Assume if we get an answer the table exists
            self.log.info('Table "%s" exists.  Skipping creation' % table)
            return
        else:
            cmd = "drop table if exists %s" % table
            cursor.execute(cmd)
            cmd = "drop table if exists %s_visit" % table
            cursor.execute(cmd)

        cmd = "create table %s (id integer primary key autoincrement, " % table
        cmd += ",".join([("%s %s" % (col, colType)) for col, colType in self.config.columns.items()])
        if len(self.config.unique) > 0:
            cmd += ", unique(" + ",".join(self.config.unique) + ")"
        cmd += ")"
        cursor.execute(cmd)

        cmd = "create table %s_visit (" % table
        cmd += ",".join([("%s %s" % (col, self.config.columns[col])) for col in self.config.visit])
        cmd += ", unique(" + ",".join(set(self.config.visit).intersection(set(self.config.unique))) + ")"
        cmd += ")"
        cursor.execute(cmd)

        conn.commit()

    def check(self, conn, info, table=None):
        """Check for the presence of a row already

        Not sure this is required, given the 'ignore' configuration option.
        """
        if table is None:
            table = self.config.table
        if self.config.ignore or len(self.config.unique) == 0:
            return False  # Our entry could already be there, but we don't care
        cursor = conn.cursor()
        sql = "SELECT COUNT(*) FROM %s WHERE " % table
        sql += " AND ".join(["%s = %s" % (col, self.placeHolder) for col in self.config.unique])
        values = [self.typemap[self.config.columns[col]](info[col]) for col in self.config.unique]

        cursor.execute(sql, values)
        if cursor.fetchone()[0] > 0:
            return True
        return False

    def addRow(self, conn, info, dryrun=False, create=False, table=None):
        """Add a row to the file table (typically 'raw').

        @param conn    Database connection
        @param info    File properties to add to database
        @param table   Name of table in database
        """
        if table is None:
            table = self.config.table
        sql = "INSERT INTO %s (%s) SELECT " % (table, ",".join(self.config.columns))
        sql += ",".join([self.placeHolder] * len(self.config.columns))
        values = [self.typemap[tt](info[col]) for col, tt in self.config.columns.items()]

        if self.config.ignore:
            sql += " WHERE NOT EXISTS (SELECT 1 FROM %s WHERE " % table
            sql += " AND ".join(["%s=%s" % (col, self.placeHolder) for col in self.config.unique])
            sql += ")"
            values += [info[col] for col in self.config.unique]

        if dryrun:
            print("Would execute: '%s' with %s" % (sql, ",".join([str(value) for value in values])))
        else:
            conn.cursor().execute(sql, values)

    def addVisits(self, conn, dryrun=False, table=None):
        """Generate the visits table (typically 'raw_visits') from the
        file table (typically 'raw').

        @param conn    Database connection
        @param table   Name of table in database
        """
        if table is None:
            table = self.config.table
        sql = "INSERT INTO %s_visit SELECT DISTINCT " % table
        sql += ",".join(self.config.visit)
        sql += " FROM %s AS vv1" % table
        sql += " WHERE NOT EXISTS "
        sql += "(SELECT vv2.visit FROM %s_visit AS vv2 WHERE vv1.visit = vv2.visit)" % (table,)
        if dryrun:
            print("Would execute: %s" % sql)
        else:
            conn.cursor().execute(sql)


class IngestConfig(Config):
    """Configuration for IngestTask"""
    parse = ConfigurableField(target=ParseTask, doc="File parsing")
    register = ConfigurableField(target=RegisterTask, doc="Registry entry")
    allowError = Field(dtype=bool, default=False, doc="Allow error in ingestion?")
    clobber = Field(dtype=bool, default=False, doc="Clobber existing file?")


class IngestTask(Task):
    """Task that will ingest images into the data repository"""
    ConfigClass = IngestConfig
    ArgumentParser = IngestArgumentParser
    _DefaultName = "ingest"

    def __init__(self, *args, **kwargs):
        super(IngestTask, self).__init__(*args, **kwargs)
        self.makeSubtask("parse")
        self.makeSubtask("register")

    @classmethod
    def parseAndRun(cls):
        """Parse the command-line arguments and run the Task"""
        config = cls.ConfigClass()
        parser = cls.ArgumentParser(name=cls._DefaultName)
        args = parser.parse_args(config)
        task = cls(config=args.config)
        task.run(args)

    def ingest(self, infile, outfile, mode="move", dryrun=False):
        """Ingest a file into the image repository.

        @param infile  Name of input file
        @param outfile Name of output file (file in repository)
        @param mode    Mode of ingest (copy/link/move/skip)
        @param dryrun  Only report what would occur?
        @param Success boolean
        """
        if mode == "skip":
            return True
        if dryrun:
            self.log.info("Would %s from %s to %s" % (mode, infile, outfile))
            return True
        try:
            outdir = os.path.dirname(outfile)
            if not os.path.isdir(outdir):
                try:
                    os.makedirs(outdir)
                except OSError:
                    # Silently ignore mkdir failures due to race conditions
                    if not os.path.isdir(outdir):
                        raise
            if os.path.lexists(outfile):
                if self.config.clobber:
                    os.unlink(outfile)
                else:
                    raise RuntimeError("File %s already exists; consider --config clobber=True" % outfile)

            if mode == "copy":
                assertCanCopy(infile, outfile)
                shutil.copyfile(infile, outfile)
            elif mode == "link":
                os.symlink(os.path.abspath(infile), outfile)
            elif mode == "move":
                assertCanCopy(infile, outfile)
                shutil.move(infile, outfile)
            else:
                raise AssertionError("Unknown mode: %s" % mode)
            self.log.info("%s --<%s>--> %s" % (infile, mode, outfile))
        except Exception as e:
            self.log.warn("Failed to %s %s to %s: %s" % (mode, infile, outfile, e))
            if not self.config.allowError:
                raise
            return False
        return True

    def isBadFile(self, filename, badFileList):
        """Return whether the file qualifies as bad

        We match against the list of bad file patterns.
        """
        filename = os.path.basename(filename)
        if not badFileList:
            return False
        for badFile in badFileList:
            if fnmatch(filename, badFile):
                return True
        return False

    def isBadId(self, info, badIdList):
        """Return whether the file information qualifies as bad

        We match against the list of bad data identifiers.
        """
        if not badIdList:
            return False
        for badId in badIdList:
            if all(info[key] == value for key, value in badId.items()):
                return True
        return False

    def expandFiles(self, fileNameList):
        """!Expand a set of filenames and globs, returning a list of filenames

        @param fileNameList A list of files and glob patterns

        N.b. globs obey Posix semantics, so a pattern that matches nothing is returned unchanged
        """
        filenameList = []
        for globPattern in fileNameList:
            files = glob(globPattern)

            if not files:               # posix behaviour is to return pattern unchanged
                self.log.warn("%s doesn't match any file" % globPattern)
                continue

            filenameList.extend(files)

        return filenameList

    def runFile(self, infile, registry, args):
        """!Examine and ingest a single file

        @param infile: File to process
        @param args: Parsed command-line arguments
        @return parsed information from FITS HDUs or None
        """
        if self.isBadFile(infile, args.badFile):
            self.log.info("Skipping declared bad file %s" % infile)
            return None
        try:
            fileInfo, hduInfoList = self.parse.getInfo(infile)
        except Exception as e:
            if not self.config.allowError:
                raise
            self.log.warn("Error parsing %s (%s); skipping" % (infile, e))
            return None
        if self.isBadId(fileInfo, args.badId.idList):
            self.log.info("Skipping declared bad file %s: %s" % (infile, fileInfo))
            return
        if registry is not None and self.register.check(registry, fileInfo):
            if args.ignoreIngested:
                return None
            self.log.warn("%s: already ingested: %s" % (infile, fileInfo))
        outfile = self.parse.getDestination(args.butler, fileInfo, infile)
        if not self.ingest(infile, outfile, mode=args.mode, dryrun=args.dryrun):
            return None
        return hduInfoList

    def run(self, args):
        """Ingest all specified files and add them to the registry"""
        filenameList = self.expandFiles(args.files)
        root = args.input
        context = self.register.openRegistry(root, create=args.create, dryrun=args.dryrun)
        with context as registry:
            for infile in filenameList:
                try:
                    hduInfoList = self.runFile(infile, registry, args)
                except Exception as exc:
                    self.log.warn("Failed to ingest file %s: %s", infile, exc)
                    continue
                if hduInfoList is None:
                    continue
                for info in hduInfoList:
                    self.register.addRow(registry, info, dryrun=args.dryrun, create=args.create)
            self.register.addVisits(registry, dryrun=args.dryrun)


def assertCanCopy(fromPath, toPath):
    """Can I copy a file?  Raise an exception is space constraints not met.

    @param fromPath    Path from which the file is being copied
    @param toPath      Path to which the file is being copied
    """
    req = os.stat(fromPath).st_size
    st = os.statvfs(os.path.dirname(toPath))
    avail = st.f_bavail * st.f_frsize
    if avail < req:
        raise RuntimeError("Insufficient space: %d vs %d" % (req, avail))
