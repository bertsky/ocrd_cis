from __future__ import absolute_import

from typing import Optional
from logging import Logger
from os.path import join

import numpy as np

from ocrd_utils import getLogger
from ocrd_models.ocrd_page import (
    AlternativeImageType,
    OcrdPage
)
from ocrd import Processor
from ocrd.processor import OcrdPageResult, OcrdPageResultImage

from .ocrolib import lineest
from .common import array2pil, check_line, determine_zoom, pil2array

class InvalidLine(Exception):
    """Line image does not allow dewarping and should be ignored."""

class InadequateLine(Exception):
    """Line image is not safe for dewarping and should be padded instead."""

# from ocropus-dewarp, but without resizing
def dewarp(image, lnorm, check=True, max_neighbour=0.02, zoom=1.0):
    if not image.width or not image.height:
        raise InvalidLine('image size is zero')
    line = pil2array(image)
    
    if np.prod(line.shape) == 0:
        raise InvalidLine('image dimensions are zero')
    if np.amax(line) == np.amin(line):
        raise InvalidLine('image is blank')
    
    temp = np.amax(line)-line # inverse, zero-closed
    if check:
        report = check_line(temp, zoom=zoom)
        if report:
            raise InadequateLine(report)
    
    temp = temp * 1.0 / np.amax(temp) # normalized
    if check:
        report = lnorm.check(temp, max_ignore=max_neighbour)
        if report:
            raise InvalidLine(report)

    lnorm.measure(temp) # find centerline
    line = lnorm.dewarp(line, cval=np.amax(line))
    
    return array2pil(line)

# pad with white above and below (as a fallback for dewarp)
def padvert(image, range_):
    line = pil2array(image)
    height = line.shape[0]
    margin = int(range_ * height / 16)
    line = np.pad(line, ((margin, margin), (0, 0)), constant_values=1.0)
    return array2pil(line)

class OcropyDewarp(Processor):
    logger: Logger

    @property
    def executable(self):
        return 'ocrd-cis-ocropy-dewarp'

    def setup(self):
        self.logger = getLogger('processor.OcropyDewarp')
        # defaults from ocrolib.lineest:
        self.lnorm = lineest.CenterNormalizer(
            params=(self.parameter['range'],
                    self.parameter['smoothness'],
                    # let's not expose this for now
                    # (otherwise we must explain mutual
                    #  dependency between smoothness
                    #  and extra params)
                    0.3))

    def process_page_pcgts(self, *input_pcgts : Optional[OcrdPage], page_id : Optional[str] = None) -> OcrdPageResult:
        """Dewarp the lines of the workspace.

        Open and deserialise PAGE input file and its respective images,
        then iterate over the element hierarchy down to the TextLine level.

        Next, get each line image according to the layout annotation (from
        the alternative image of the line, or by cropping via coordinates
        into the higher-level image), and dewarp it (without resizing).
        Export the result as an image file.

        Add the new image file to the workspace along with the output fileGrp,
        and using a file ID with suffix ``.IMG-DEWARP`` along with further
        identification of the input element.

        Reference each new image in the AlternativeImage of the element.

        Produce a new output file by serialising the resulting hierarchy.
        """
        pcgts = input_pcgts[0]
        result = OcrdPageResult(pcgts)
        page = pcgts.get_Page()

        page_image, page_xywh, page_image_info = self.workspace.image_from_page(
            page, page_id)
        zoom = determine_zoom(self.logger, page_id, self.parameter['dpi'], page_image_info)

        regions = page.get_AllRegions(classes=['Text'], order='reading-order')
        if not regions:
            self.logger.warning('Page "%s" contains no text regions', page_id)
        for region in regions:
            region_image, region_xywh = self.workspace.image_from_segment(
                region, page_image, page_xywh)

            lines = region.get_TextLine()
            if not lines:
                self.logger.warning('Region %s contains no text lines', region.id)
            for line in lines:
                line_image, line_xywh = self.workspace.image_from_segment(
                    line, region_image, region_xywh)

                self.logger.info("About to dewarp page '%s' region '%s' line '%s'",
                                 page_id, region.id, line.id)
                try:
                    dew_image = dewarp(line_image, self.lnorm, check=True,
                                       max_neighbour=self.parameter['max_neighbour'],
                                       zoom=zoom)
                except InvalidLine as err:
                    self.logger.error('cannot dewarp line "%s": %s', line.id, err)
                    continue
                except InadequateLine as err:
                    self.logger.warning('cannot dewarp line "%s": %s', line.id, err)
                    # as a fallback, simply pad the image vertically
                    # (just as dewarping would do on average, so at least
                    #  this line has similar margins as the others):
                    dew_image = padvert(line_image, self.parameter['range'])
                # update PAGE (reference the image file):
                alt_image = AlternativeImageType(comments=line_xywh['features'] + ',dewarped')
                line.add_AlternativeImage(alternative_image)
                return OcrdPageResultImage(dew_image, region.id + '_' + line.id + '.IMG-DEWARP', alt_image)
