#
# LSST Data Management System
# Copyright 2008-2015 AURA/LSST.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.    See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <http://www.lsstcorp.org/LegalNotices/>.
#
import lsst.pex.config as pexConfig
import lsst.afw.geom as afwGeom
import lsst.afw.image as afwImage
import lsst.daf.persistence
import lsst.pipe.base as pipeBase
import lsst.meas.algorithms as measAlg

from lsst.afw.fits import FitsError
from lsst.coadd.utils import CoaddDataIdContainer
from .selectImages import WcsSelectImagesTask, SelectStruct
from .coaddInputRecorder import CoaddInputRecorderTask
from .scaleVariance import ScaleVarianceTask

__all__ = ["CoaddBaseTask", "getSkyInfo"]


class CoaddBaseConfig(pexConfig.Config):
    """!Configuration parameters for CoaddBaseTask

    @anchor CoaddBaseConfig_

    @brief Configuration parameters shared between MakeCoaddTempExp and AssembleCoadd
    """
    coaddName = pexConfig.Field(
        doc="Coadd name: typically one of deep or goodSeeing.",
        dtype=str,
        default="deep",
    )
    select = pexConfig.ConfigurableField(
        doc="Image selection subtask.",
        target=WcsSelectImagesTask,
    )
    badMaskPlanes = pexConfig.ListField(
        dtype=str,
        doc="Mask planes that, if set, the associated pixel should not be included in the coaddTempExp.",
        default=("NO_DATA",),
    )
    inputRecorder = pexConfig.ConfigurableField(
        doc="Subtask that helps fill CoaddInputs catalogs added to the final Exposure",
        target=CoaddInputRecorderTask
    )
    doPsfMatch = pexConfig.Field(
        dtype=bool,
        doc="Match to modelPsf? Deprecated. Sets makePsfMatched=True, makeDirect=False",
        default=False
    )
    modelPsf = measAlg.GaussianPsfFactory.makeField(doc="Model Psf factory")
    doApplyUberCal = pexConfig.Field(
        dtype=bool,
        doc="Apply jointcal WCS and PhotoCalib results to input calexps?",
        default=False
    )
    useMeasMosaic = pexConfig.Field(
        dtype=bool,
        doc="Use meas_mosaic's applyMosaicResultsExposure() to do the photometric "
        "calibration/wcs update (deprecated).",
        default=False
    )
    matchingKernelSize = pexConfig.Field(
        dtype=int,
        doc="Size in pixels of matching kernel. Must be odd.",
        default=21,
        check=lambda x: x % 2 == 1
    )


class CoaddTaskRunner(pipeBase.TaskRunner):

    @staticmethod
    def getTargetList(parsedCmd, **kwargs):
        return pipeBase.TaskRunner.getTargetList(parsedCmd, selectDataList=parsedCmd.selectId.dataList,
                                                 **kwargs)


class CoaddBaseTask(pipeBase.CmdLineTask):
    """!Base class for coaddition.

    Subclasses must specify _DefaultName
    """
    ConfigClass = CoaddBaseConfig
    RunnerClass = CoaddTaskRunner

    def __init__(self, *args, **kwargs):
        pipeBase.Task.__init__(self, *args, **kwargs)
        self.makeSubtask("select")
        self.makeSubtask("inputRecorder")

    def selectExposures(self, patchRef, skyInfo=None, selectDataList=[]):
        """!
        @brief Select exposures to coadd

        Get the corners of the bbox supplied in skyInfo using @ref afwGeom.Box2D and convert the pixel
        positions of the bbox corners to sky coordinates using @ref skyInfo.wcs.pixelToSky. Use the
        @ref WcsSelectImagesTask_ "WcsSelectImagesTask" to select exposures that lie inside the patch
        indicated by the dataRef.

        @param[in] patchRef  data reference for sky map patch. Must include keys "tract", "patch",
                             plus the camera-specific filter key (e.g. "filter" or "band")
        @param[in] skyInfo   geometry for the patch; output from getSkyInfo
        @return    a list of science exposures to coadd, as butler data references
        """
        if skyInfo is None:
            skyInfo = self.getSkyInfo(patchRef)
        cornerPosList = afwGeom.Box2D(skyInfo.bbox).getCorners()
        coordList = [skyInfo.wcs.pixelToSky(pos) for pos in cornerPosList]
        return self.select.runDataRef(patchRef, coordList, selectDataList=selectDataList).dataRefList

    def getSkyInfo(self, patchRef):
        """!
        @brief Use @ref getSkyinfo to return the skyMap, tract and patch information, wcs and the outer bbox
        of the patch.

        @param[in] patchRef  data reference for sky map. Must include keys "tract" and "patch"

        @return pipe_base Struct containing:
        - skyMap: sky map
        - tractInfo: information for chosen tract of sky map
        - patchInfo: information about chosen patch of tract
        - wcs: WCS of tract
        - bbox: outer bbox of patch, as an afwGeom Box2I
        """
        return getSkyInfo(coaddName=self.config.coaddName, patchRef=patchRef)

    def getCalibratedExposure(self, dataRef, bgSubtracted):
        """Return one calibrated Exposure, possibly with an updated SkyWcs.

        @param[in] dataRef        a sensor-level data reference
        @param[in] bgSubtracted   return calexp with background subtracted? If False get the
                                  calexp's background background model and add it to the calexp.
        @return calibrated exposure

        If config.doApplyUberCal, the exposure will be photometrically
        calibrated via the `jointcal_photoCalib` dataset and have its SkyWcs
        updated to the `jointcal_wcs`, otherwise it will be calibrated via the
        Exposure's own Calib and have the original SkyWcs.
        """
        exposure = dataRef.get("calexp", immediate=True)
        if not bgSubtracted:
            background = dataRef.get("calexpBackground", immediate=True)
            mi = exposure.getMaskedImage()
            mi += background.getImage()
            del mi

        if self.config.doApplyUberCal:
            if self.config.useMeasMosaic:
                try:
                    from lsst.meas.mosaic import applyMosaicResultsExposure
                    # NOTE: this changes exposure in-place, updating its Calib and Wcs.
                    # Save the calibration error, as it gets overwritten with zero.
                    fluxMag0Err = exposure.getCalib().getFluxMag0()[1]
                    applyMosaicResultsExposure(dataRef, calexp=exposure)
                    fluxMag0 = exposure.getCalib().getFluxMag0()[0]
                    photoCalib = afwImage.PhotoCalib(1.0/fluxMag0,
                                                     fluxMag0Err/fluxMag0**2,
                                                     exposure.getBBox())
                except ImportError:
                    msg = "Cannot apply meas_mosaic calibration: meas_mosaic not available."
                    raise RuntimeError(msg.format(dataRef.dataId))
            else:
                try:
                    photoCalib = dataRef.get("jointcal_photoCalib")
                except lsst.daf.persistence.NoResults:
                    msg = "Cannot apply jointcal calibration: `jointcal_photoCalib` not found for dataRef {}."
                    raise RuntimeError(msg.format(dataRef.dataId))
                try:
                    skyWcs = dataRef.get("jointcal_wcs")
                    exposure.setWcs(skyWcs)
                except lsst.daf.persistence.NoResults:
                    msg = "Cannot update to jointcal SkyWcs: `jointcal_wcs` not found for dataRef {}."
                    raise RuntimeError(msg.format(dataRef.dataId))
        else:
            fluxMag0 = exposure.getCalib().getFluxMag0()
            photoCalib = afwImage.PhotoCalib(1.0/fluxMag0[0],
                                             fluxMag0[1]/fluxMag0[0]**2,
                                             exposure.getBBox())

        exposure.maskedImage = photoCalib.calibrateImage(exposure.maskedImage)
        exposure.maskedImage /= photoCalib.getCalibrationMean()
        exposure.setCalib(afwImage.Calib(1/photoCalib.getCalibrationMean()))
        # TODO: The images will have a calibration of 1.0 everywhere once RFC-545 is implemented.
        # exposure.setCalib(afwImage.Calib(1.0))
        return exposure

    def getCoaddDatasetName(self, warpType="direct"):
        """Return coadd name for given warpType and task config

        Parameters
        ----------
        warpType : string
            Either 'direct' or 'psfMatched'

        Returns
        -------
        CoaddDatasetName : `string`
        """
        suffix = "" if warpType == "direct" else warpType[0].upper() + warpType[1:]
        return self.config.coaddName + "Coadd" + suffix

    def getTempExpDatasetName(self, warpType="direct"):
        """Return warp name for given warpType and task config

        Parameters
        ----------
        warpType : string
            Either 'direct' or 'psfMatched'

        Returns
        -------
        WarpDatasetName : `string`
        """
        return self.config.coaddName + "Coadd_" + warpType + "Warp"

    @classmethod
    def _makeArgumentParser(cls):
        """Create an argument parser
        """
        parser = pipeBase.ArgumentParser(name=cls._DefaultName)
        parser.add_id_argument("--id", "deepCoadd", help="data ID, e.g. --id tract=12345 patch=1,2",
                               ContainerClass=CoaddDataIdContainer)
        parser.add_id_argument("--selectId", "calexp", help="data ID, e.g. --selectId visit=6789 ccd=0..9",
                               ContainerClass=SelectDataIdContainer)
        return parser

    def _getConfigName(self):
        """Return the name of the config dataset
        """
        return "%s_%s_config" % (self.config.coaddName, self._DefaultName)

    def _getMetadataName(self):
        """Return the name of the metadata dataset
        """
        return "%s_%s_metadata" % (self.config.coaddName, self._DefaultName)

    def getBadPixelMask(self):
        """!
        @brief Convenience method to provide the bitmask from the mask plane names
        """
        return afwImage.Mask.getPlaneBitMask(self.config.badMaskPlanes)


class SelectDataIdContainer(pipeBase.DataIdContainer):
    """!
    @brief A dataId container for inputs to be selected.

    Read the header (including the size and Wcs) for all specified
    inputs and pass those along, ultimately for the SelectImagesTask.
    This is most useful when used with multiprocessing, as input headers are
    only read once.
    """

    def makeDataRefList(self, namespace):
        """Add a dataList containing useful information for selecting images"""
        super(SelectDataIdContainer, self).makeDataRefList(namespace)
        self.dataList = []
        for ref in self.refList:
            try:
                md = ref.get("calexp_md", immediate=True)
                wcs = afwGeom.makeSkyWcs(md)
                data = SelectStruct(dataRef=ref, wcs=wcs, bbox=afwImage.bboxFromMetadata(md))
            except FitsError:
                namespace.log.warn("Unable to construct Wcs from %s" % (ref.dataId))
                continue
            self.dataList.append(data)


def getSkyInfo(coaddName, patchRef):
    """!
    @brief Return the SkyMap, tract and patch information, wcs, and outer bbox of the patch to be coadded.

    @param[in]  coaddName  coadd name; typically one of deep or goodSeeing
    @param[in]  patchRef   data reference for sky map. Must include keys "tract" and "patch"

    @return pipe_base Struct containing:
    - skyMap: sky map
    - tractInfo: information for chosen tract of sky map
    - patchInfo: information about chosen patch of tract
    - wcs: WCS of tract
    - bbox: outer bbox of patch, as an afwGeom Box2I
    """
    skyMap = patchRef.get(coaddName + "Coadd_skyMap")
    tractId = patchRef.dataId["tract"]
    tractInfo = skyMap[tractId]

    # patch format is "xIndex,yIndex"
    patchIndex = tuple(int(i) for i in patchRef.dataId["patch"].split(","))
    patchInfo = tractInfo.getPatchInfo(patchIndex)

    return pipeBase.Struct(
        skyMap=skyMap,
        tractInfo=tractInfo,
        patchInfo=patchInfo,
        wcs=tractInfo.getWcs(),
        bbox=patchInfo.getOuterBBox(),
    )


def scaleVariance(maskedImage, maskPlanes, log=None):
    """!
    @brief Scale the variance in a maskedImage

    The variance plane in a convolved or warped image (or a coadd derived
    from warped images) does not accurately reflect the noise properties of
    the image because variance has been lost to covariance. This function
    attempts to correct for this by scaling the variance plane to match
    the observed variance in the image. This is not perfect (because we're
    not tracking the covariance) but it's simple and is often good enough.

    @deprecated Use the ScaleVarianceTask instead.

    @param maskedImage  MaskedImage to operate on; variance will be scaled
    @param maskPlanes  List of mask planes for pixels to reject
    @param log  Log for reporting the renormalization factor; or None
    @return renormalisation factor
    """
    config = ScaleVarianceTask.ConfigClass()
    config.maskPlanes = maskPlanes
    task = ScaleVarianceTask(config=config, name="scaleVariance", log=log)
    return task.run(maskedImage)
