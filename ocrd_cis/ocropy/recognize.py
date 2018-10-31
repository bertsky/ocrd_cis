from __future__ import absolute_import

import sys, os.path, cv2
import numpy as np
from PIL import Image


from ocrd.utils import getLogger, concat_padded, xywh_from_points, points_from_x0y0x1y1
from ocrd.model.ocrd_page import from_file, to_xml, TextEquivType, CoordsType, GlyphType
from ocrd import Processor, MIMETYPE_PAGE

from ocrd_cis import get_ocrd_tool

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from ocrd_cis.ocropy import ocrolib
from ocrd_cis.ocropy.ocrolib import lstm



def bounding_box(coord_points):
    point_list = [[int(p) for p in pair.split(',')] for pair in coord_points.split(' ')]
    x_coordinates, y_coordinates = zip(*point_list)
    return (min(x_coordinates), min(y_coordinates), max(x_coordinates), max(y_coordinates))


def resize_keep_ratio(image, baseheight=48):
    hpercent = (baseheight / float(image.size[1]))
    wsize = int((float(image.size[0] * float(hpercent))))
    image = image.resize((wsize, baseheight), Image.ANTIALIAS)
    return image


def binarize(pil_image):
    # Convert RGB to OpenCV
    img = cv2.cvtColor(np.asarray(pil_image), cv2.COLOR_RGB2GRAY)

    # global thresholding
    #ret1,th1 = cv2.threshold(img,127,255,cv2.THRESH_BINARY)

    # Otsu's thresholding
    #ret2,th2 = cv2.threshold(img,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)

    # Otsu's thresholding after Gaussian filtering
    blur = cv2.GaussianBlur(img,(5,5),0)
    ret3,th3 = cv2.threshold(blur,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)

    bin_img = Image.fromarray(th3)
    return bin_img

def deletefile(file):
    if os.path.exists(file):
        os.remove(file)

def process1(arg):
    fname,parallel,pad,lnorm,network = arg
    base,_ = ocrolib.allsplitext(fname)
    line = ocrolib.read_image_gray(fname)
    raw_line = line.copy()
    if np.prod(line.shape)==0: return None
    if np.amax(line)==np.amin(line): return None

    temp = np.amax(line)-line
    temp = temp*1.0/np.amax(temp)
    lnorm.measure(temp)
    line = lnorm.normalize(line,cval=np.amax(line))

    line = lstm.prepare_line(line,pad)
    pred = network.predictString(line)


    # getting confidence
    result = lstm.translate_back(network.outputs,pos=1)
    scale = len(raw_line.T)*1.0/(len(network.outputs)-2*pad)
    for r,c in result:
        if c == 0:
            confid = network.outputs[r,c]
            c = network.l2s([c])
            r = (r-pad)*scale
        else:
            confid = 0

    return str(pred), confid



class OcropyRecognize(Processor):

    def __init__(self, *args, **kwargs):
        ocrd_tool = get_ocrd_tool()
        kwargs['ocrd_tool'] = ocrd_tool['tools']['cis-ocrd-ocropy-recognize']
        kwargs['version'] = ocrd_tool['version']
        super(OcropyRecognize, self).__init__(*args, **kwargs)
        self.log = getLogger('OcropyRecognize')


    def process(self):
        """
        Performs the (text) recognition.
        """
        #print(self.parameter)
        if self.parameter['textequiv_level'] not in ['line', 'word', 'glyph']:
            raise Exception("currently only implemented at the line/glyph level")
        
        filepath = os.path.dirname(os.path.abspath(__file__))
        model = 'fraktur.pyrnn.gz' # default model
        modelpath = os.path.join(filepath,'models',model)

        
        #checks if model is in default path and loads it
        if 'model' in self.parameter:
            model = self.parameter['model']
            modelpath = filepath + '/models/' + model + '.gz'
            if os.path.isfile(modelpath) == False:
                raise Exception("configured model " + model + " is not in models folder")
                sys.exit(1)

            else:
                network = ocrolib.load_object(modelpath,verbose=1)
                for x in network.walk(): x.postLoad()
                for x in network.walk():
                    if isinstance(x,lstm.LSTM):
                        x.allocate(5000)

        lnorm = getattr(network,"lnorm",None)

        pad=16   #default: 16
        parallel=0

        #self.log.info("Using model %s in %s for recognition", model)
        for (n, input_file) in enumerate(self.input_files):
            #self.log.info("INPUT FILE %i / %s", n, input_file)
            pcgts = from_file(self.workspace.download_file(input_file))
            pil_image = self.workspace.resolve_image_as_pil(pcgts.get_Page().imageFilename)


            self.log.info("page %s", pcgts)
            for region in pcgts.get_Page().get_TextRegion():
                textlines = region.get_TextLine()
                self.log.info("About to recognize text in %i lines of region '%s'", len(textlines), region.id)


                for line in textlines:
                    self.log.debug("Recognizing text in line '%s'", line.id)
                    
                    #get box from points
                    box = bounding_box(line.get_Coords().points)
                        
                    #crop word from page
                    croped_image = pil_image.crop(box=box)

                    #binarize with Otsu's thresholding after Gaussian filtering
                    bin_image = binarize(croped_image)

                    #resize image to 48 pixel height
                    final_img = resize_keep_ratio(bin_image)

                    #save temp image
                    imgpath = os.path.join(filepath, 'temp/temp.png')
                    final_img.save(imgpath)

                     #process ocropy
                    ocropyoutput, confidence = process1([imgpath,parallel,pad,lnorm,network])

                    line.add_TextEquiv(TextEquivType(Unicode=ocropyoutput))
                    print(ocropyoutput)
                    deletefile(imgpath)        
                    
                    wordconflist = []
                    linewords = line.get_Word()

                    for wnum, word in enumerate(linewords):
                        if self.parameter['textequiv_level'] == 'word':
                            self.log.debug("Recognizing text in word '%s'", word.id)

                            #get box from points
                            box = bounding_box(word.get_Coords().points)
                            
                            #crop word from page
                            croped_image = pil_image.crop(box=box)

                            #binarize with Otsu's thresholding after Gaussian filtering
                            bin_image = binarize(croped_image)

                            #resize image to 48 pixel height
                            final_img = resize_keep_ratio(bin_image)

                            #save temp image
                            imgpath = os.path.join(filepath, 'temp/temp.png')
                            final_img.save(imgpath)

                            #process ocropy
                            ocropyoutput, confidence = process1([imgpath,parallel,pad,lnorm,network])

                            word.add_TextEquiv(TextEquivType(Unicode=ocropyoutput))

                            print(ocropyoutput)


                        glyphconflist = []
                        wordglyphs = word.get_Glyph()


                        for gnum, glyph in enumerate(wordglyphs):
                            if self.parameter['textequiv_level'] == 'glyph':
                                self.log.debug("Recognizing text in glyph '%s'", glyph.id)

                            #get box from points
                            box = bounding_box(glyph.get_Coords().points)
                            
                            #crop word from page
                            croped_image = pil_image.crop(box=box)

                            #binarize with Otsu's thresholding after Gaussian filtering
                            bin_image = binarize(croped_image)

                            #resize image to 48 pixel height
                            final_img = resize_keep_ratio(bin_image)

                            #save temp image
                            imgpath = os.path.join(filepath, 'temp/temp.png')
                            final_img.save(imgpath)

                            #process ocropy
                            ocropyoutput, confidence = process1([imgpath,parallel,pad,lnorm,network])

                            if self.parameter['textequiv_level'] == 'glyph':

                                glyph.add_TextEquiv(TextEquivType(Unicode=ocropyoutput))
                                glyph.add_TextEquiv(TextEquivType(conf=confidence))

                            glyphconflist.append(confidence)

                            if gnum == len(wordglyphs)-1 and glyphconflist != []:
                                wordconf = (min(glyphconflist) + max(glyphconflist))/2
                                word.add_TextEquiv(TextEquivType(conf=wordconf))
                                wordconflist.append(wordconf)

                            if wnum == len(linewords)-1 and wordconflist != []:
                                lineconf = (min(wordconflist) + max(wordconflist))/2
                                line.add_TextEquiv(TextEquivType(conf=lineconf))


                            print(ocropyoutput)



            ID = concat_padded(self.output_file_grp, n)
            self.add_output_file(
                ID=ID,
                file_grp=self.output_file_grp,
                basename=ID + '.xml',
                mimetype=MIMETYPE_PAGE,
                content=to_xml(pcgts),
            )
