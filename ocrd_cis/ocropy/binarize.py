from __future__ import absolute_import
from logging import Logger

import os.path
import PIL
import cv2
import numpy as np
from PIL import Image
from os.path import join

#import kraken.binarization

from ocrd_utils import (
    getLogger,
    make_file_id,
    assert_file_grp_cardinality,
    MIMETYPE_PAGE
)
from ocrd_modelfactory import page_from_file
from ocrd_models.ocrd_page import (
    OcrdPage, to_xml, AlternativeImageType
)
from ocrd import Processor

from . import common
from .common import (
    # binarize,
     array2pil, determine_zoom, pil2array, remove_noise)

#sys.path.append(os.path.dirname(os.path.abspath(__file__)))

def binarize(pil_image, method='ocropy', maxskew=2, threshold=0.5, nrm=False, zoom=1.0):
    LOG = getLogger('processor.OcropyBinarize')
    LOG.debug('binarizing %dx%d image with method=%s', pil_image.width, pil_image.height, method)
    if method == 'none':
        # useful if the images are already binary,
        # but lack image attribute `binarized`
        return pil_image, 0
    elif method == 'ocropy':
        # parameter defaults from ocropy-nlbin:
        array = pil2array(pil_image)
        bin, angle = common.binarize(array, maxskew=maxskew, threshold=threshold, nrm=nrm, zoom=zoom)
        return array2pil(bin), angle
    # equivalent to ocropy, but without deskewing:
    # elif method == 'kraken':
    #     image = kraken.binarization.nlbin(pil_image)
    #     return image, 0
    # FIXME: add 'sauvola' from OLD/ocropus-sauvola
    else:
        # Convert RGB to OpenCV
        #img = cv2.cvtColor(np.asarray(pil_image), cv2.COLOR_RGB2GRAY)
        img = np.asarray(pil_image.convert('L'))

        if method == 'global':
            # global thresholding
            _, th = cv2.threshold(img,threshold*255,255,cv2.THRESH_BINARY)
        elif method == 'otsu':
            # Otsu's thresholding
            _, th = cv2.threshold(img,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)
        elif method == 'gauss-otsu':
            # Otsu's thresholding after Gaussian filtering
            blur = cv2.GaussianBlur(img, (5, 5), 0)
            _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
        else:
            raise Exception('unknown binarization method %s' % method)
        return Image.fromarray(th), 0

class OcropyBinarize(Processor):
    logger: Logger

    @property
    def executable(self):
        return 'ocrd-cis-ocropy-binarize'

    def setup(self):
        self.logger = getLogger('processor.OcropyBinarize')
        assert_file_grp_cardinality(self.input_file_grp, 1)
        assert_file_grp_cardinality(self.output_file_grp, 1)
        method = self.parameter['method']
        if self.parameter['grayscale'] and method != 'ocropy':
            self.logger.critical(f'Requested method {method} does not support grayscale normalized output')
            raise Exception('only method=ocropy allows grayscale=true')

    def process_page_pcgts(self, *input_pcgts, output_file_id: str = None, page_id: str = None) -> OcrdPage:
        """Binarize (and optionally deskew/despeckle) the pages/regions/lines of the workspace.

        THEN Iterate over the PAGE-XML element hierarchy down to the requested
        ``level-of-operation``.

        Next, for each file, crop each segment image according to the layout
        annotation (via coordinates into the higher-level image, or from the
        alternative image), and determine the threshold for binarization and
        the deskewing angle of the segment (up to ``maxskew``). Then despeckle
        by removing connected components smaller than ``noise_maxsize``.
        Finally, apply results to the image and export it as an image file.

        Add the new image file to the workspace along with the output fileGrp,
        and using a file ID with suffix ``.IMG-BIN`` along with further
        identification of the input element.

        Reference each new image in the AlternativeImage of the element.

        Return a PAGE-XML with AlternativeImage and the arguments for ``workspace.save_image_file``.
        """
        level = self.parameter['level-of-operation']
        assert self.workspace
        self.logger.debug(f'Level of operation: "{level}"')

        pcgts = input_pcgts[0]
        page = pcgts.get_Page()
        assert page

        page_image, page_xywh, page_image_info = self.workspace.image_from_page(page, page_id, feature_filter='binarized')
        zoom = determine_zoom(self.parameter['dpi'], page_image_info)
        self.logger.info(f"Page '{page_id}' uses {self.parameter['dpi']} DPI")
        
        ret = [pcgts]
        if level == 'page':
            try:
                ret.append(self.process_page(page, page_image, page_xywh, zoom, page_id, output_file_id))
            except ValueError as e:
                self.logger.exception(e)
        else:
            # TODO
            raise NotImplementedError
            if level == 'table':
                regions = page.get_TableRegion()
            else: # region
                regions = page.get_AllRegions(classes=['Text'], order='reading-order')
            if not regions:
                self.logger.warning('Page "%s" contains no text regions', page_id)
            for region in regions:
                region_image, region_xywh = self.workspace.image_from_segment(
                    region, page_image, page_xywh, feature_filter='binarized')
                if level == 'region':
                    self.process_region(region, region_image, region_xywh, zoom,
                                        input_file.pageId, file_id + '_' + region.id)
                    continue
                lines = region.get_TextLine()
                if not lines:
                    self.logger.warning('Page "%s" region "%s" contains no text lines',
                                        page_id, region.id)
                for line in lines:
                    line_image, line_xywh = self.workspace.image_from_segment(
                        line, region_image, region_xywh, feature_filter='binarized')
                    self.process_line(line, line_image, line_xywh, zoom,
                                      input_file.pageId, region.id,
                                      file_id + '_' + region.id + '_' + line.id)

        return ret

    def process_page(self, page, page_image, page_xywh, zoom, page_id, file_id) -> tuple[Image.Image, str, str]:
        if not page_image.width or not page_image.height:
            raise ValueError("Skipping page '%s' with zero size", page_id)
        self.logger.info("About to binarize page '%s'", page_id)
        assert self.output_file_grp

        features = page_xywh['features']
        if 'angle' in page_xywh and page_xywh['angle']:
            # orientation has already been annotated (by previous deskewing),
            # so skip deskewing here:
            maxskew = 0
        else:
            maxskew = self.parameter['maxskew']
        bin_image, angle = binarize(page_image,
                                    method=self.parameter['method'],
                                    maxskew=maxskew,
                                    threshold=self.parameter['threshold'],
                                    nrm=self.parameter['grayscale'],
                                    zoom=zoom)
        if angle:
            features += ',deskewed'
        page_xywh['angle'] = angle
        if self.parameter['noise_maxsize']:
            bin_image = remove_noise(
                bin_image, maxsize=self.parameter['noise_maxsize'])
            features += ',despeckled'
        # annotate angle in PAGE (to allow consumers of the AlternativeImage
        # to do consistent coordinate transforms, and non-consumers
        # to redo the rotation themselves):
        orientation = -page_xywh['angle']
        orientation = 180 - (180 - orientation) % 360 # map to [-179.999,180]
        page.set_orientation(orientation)
        # update METS (add the image file):
        if self.parameter['grayscale']:
            file_id += '.IMG-NRM'
            features += ',grayscale_normalized'
        else:
            file_id += '.IMG-BIN'
            features += ',binarized'
        bin_image_path = join(self.output_file_grp, f'{file_id}.png')
        # update PAGE (reference the image file):
        page.add_AlternativeImage(AlternativeImageType(filename=bin_image_path, comments=features))
        return (bin_image, file_id, bin_image_path)

    def process_region(self, region, region_image, region_xywh, zoom, page_id, file_id):
        if not region_image.width or not region_image.height:
            self.logger.warning("Skipping region '%s' with zero size", region.id)
            return
        self.logger.info("About to binarize page '%s' region '%s'", page_id, region.id)
        features = region_xywh['features']
        if 'angle' in region_xywh and region_xywh['angle']:
            # orientation has already been annotated (by previous deskewing),
            # so skip deskewing here:
            bin_image, _ = binarize(region_image,
                                    method=self.parameter['method'],
                                    maxskew=0,
                                    nrm=self.parameter['grayscale'],
                                    zoom=zoom)
        else:
            bin_image, angle = binarize(region_image,
                                        method=self.parameter['method'],
                                        maxskew=self.parameter['maxskew'],
                                        nrm=self.parameter['grayscale'],
                                        zoom=zoom)
            if angle:
                features += ',deskewed'
            region_xywh['angle'] = angle
        bin_image = remove_noise(bin_image,
                                 maxsize=self.parameter['noise_maxsize'])
        if self.parameter['noise_maxsize']:
            features += ',despeckled'
        # annotate angle in PAGE (to allow consumers of the AlternativeImage
        # to do consistent coordinate transforms, and non-consumers
        # to redo the rotation themselves):
        orientation = -region_xywh['angle']
        orientation = 180 - (180 - orientation) % 360 # map to [-179.999,180]
        region.set_orientation(orientation)
        # update METS (add the image file):
        if self.parameter['grayscale']:
            file_id += '.IMG-NRM'
            features += ',grayscale_normalized'
        else:
            file_id += '.IMG-BIN'
            features += ',binarized'
        file_path = self.workspace.save_image_file(
            bin_image, file_id, self.output_file_grp,
            page_id=page_id)
        # update PAGE (reference the image file):
        region.add_AlternativeImage(AlternativeImageType(
            filename=file_path,
            comments=features))

    def process_line(self, line, line_image, line_xywh, zoom, page_id, region_id, file_id):
        if not line_image.width or not line_image.height:
            self.logger.warning("Skipping line '%s' with zero size", line.id)
            return
        self.logger.info("About to binarize page '%s' region '%s' line '%s'",
                         page_id, region_id, line.id)
        features = line_xywh['features']
        bin_image, angle = binarize(line_image,
                                    method=self.parameter['method'],
                                    maxskew=self.parameter['maxskew'],
                                    nrm=self.parameter['grayscale'],
                                    zoom=zoom)
        if angle:
            features += ',deskewed'
        # annotate angle in PAGE (to allow consumers of the AlternativeImage
        # to do consistent coordinate transforms, and non-consumers
        # to redo the rotation themselves):
        #orientation = -angle
        #orientation = 180 - (180 - orientation) % 360 # map to [-179.999,180]
        #line.set_orientation(orientation) # does not exist on line level!
        self.logger.warning("cannot add orientation %.2f to page '%s' region '%s' line '%s'",
                            -angle, page_id, region_id, line.id)
        bin_image = remove_noise(bin_image,
                                 maxsize=self.parameter['noise_maxsize'])
        if self.parameter['noise_maxsize']:
            features += ',despeckled'
        # update METS (add the image file):
        if self.parameter['grayscale']:
            file_id += '.IMG-NRM'
            features += ',grayscale_normalized'
        else:
            file_id += '.IMG-BIN'
            features += ',binarized'
        file_path = self.workspace.save_image_file(
            bin_image, file_id, self.output_file_grp,
            page_id=page_id)
        # update PAGE (reference the image file):
        line.add_AlternativeImage(AlternativeImageType(
            filename=file_path,
            comments=features))
