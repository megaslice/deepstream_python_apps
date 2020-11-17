#!/usr/bin/env python3

################################################################################
# Copyright (c) 2019-2020, NVIDIA CORPORATION. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
################################################################################

import sys, platform, threading, time
import gi, pyds
gi.require_version('Gst', '1.0')
from gi.repository import GObject, Gst

def is_aarch64():
    return platform.uname()[4] == 'aarch64'

def bus_call(bus, message, loop):
    t = message.type
    if t == Gst.MessageType.EOS:
        sys.stdout.write("End-of-stream\n")
        loop.quit()
    elif t==Gst.MessageType.WARNING:
        err, debug = message.parse_warning()
        sys.stderr.write("Warning: %s: %s\n" % (err, debug))
    elif t == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        sys.stderr.write("Error: %s: %s\n" % (err, debug))
        loop.quit()
    return True

PGIE_CLASS_ID_VEHICLE = 0
PGIE_CLASS_ID_BICYCLE = 1
PGIE_CLASS_ID_PERSON = 2
PGIE_CLASS_ID_ROADSIGN = 3

class DeepstreamPythonDetectorUnit:

    def osd_sink_pad_buffer_probe(self, pad, info, u_data):
        self.probing_yet = True
        
        frame_number = 0
        # Intialising object counter with 0.
        obj_counter = {
            PGIE_CLASS_ID_VEHICLE: 0,
            PGIE_CLASS_ID_PERSON: 0,
            PGIE_CLASS_ID_BICYCLE: 0,
            PGIE_CLASS_ID_ROADSIGN: 0
        }
        num_rects = 0

        gst_buffer = info.get_buffer()
        if not gst_buffer:
            print("Unable to get GstBuffer ")
            return

        # Retrieve batch metadata from the gst_buffer
        # Note that pyds.gst_buffer_get_nvds_batch_meta() expects the
        # C address of gst_buffer as input, which is obtained with hash(gst_buffer)
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
        l_frame = batch_meta.frame_meta_list
        while l_frame is not None:
            try:
                # Note that l_frame.data needs a cast to pyds.NvDsFrameMeta
                # The casting is done by pyds.NvDsFrameMeta.cast()
                # The casting also keeps ownership of the underlying memory
                # in the C code, so the Python garbage collector will leave
                # it alone.
                print("getting frame data")
                frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            except StopIteration:
                break

            frame_number=frame_meta.frame_num
            num_rects = frame_meta.num_obj_meta
            l_obj=frame_meta.obj_meta_list
            print("iterating objects")
            while l_obj is not None:
                try:
                    # Casting l_obj.data to pyds.NvDsObjectMeta
                    obj_meta=pyds.NvDsObjectMeta.cast(l_obj.data)
                except StopIteration:
                    break
                self.running_list.append(obj_meta)
                obj_counter[obj_meta.class_id] += 1
                try: 
                    l_obj=l_obj.next
                except StopIteration:
                    break

            display_text = "Frame Number={} Number of Objects={} Vehicle_count={} Person_count={}".format(frame_number, num_rects, obj_counter[PGIE_CLASS_ID_VEHICLE], obj_counter[PGIE_CLASS_ID_PERSON])
            print(display_text)

            try:
                l_frame=l_frame.next
            except StopIteration:
                break
			    
        return Gst.PadProbeReturn.OK	


    def __init__(self, sourcedev = "/dev/video0", headless = False):
        # Standard GStreamer initialization
        GObject.threads_init()
        Gst.init(None)

        # Create gstreamer elements
        # Create Pipeline element that will form a connection of other elements
        print("Creating Pipeline \n ")
        self.pipeline = Gst.Pipeline()

        if not self.pipeline:
            sys.stderr.write(" Unable to create Pipeline")

        # Source element for reading from the file
        print("Creating Source \n ")
        source = Gst.ElementFactory.make("v4l2src", "usb-cam-source")
        if not source:
            sys.stderr.write(" Unable to create Source")

        caps_v4l2src = Gst.ElementFactory.make("capsfilter", "v4l2src_caps")
        if not caps_v4l2src:
            sys.stderr.write(" Unable to create v4l2src capsfilter")


        print("Creating Video Converter")

        # Adding videoconvert -> nvvideoconvert as not all
        # raw formats are supported by nvvideoconvert;
        # Say YUYV is unsupported - which is the common
        # raw format for many logi usb cams
        # In case we have a camera with raw format supported in
        # nvvideoconvert, GStreamer plugins' capability negotiation
        # shall be intelligent enough to reduce compute by
        # videoconvert doing passthrough (TODO we need to confirm this)

        # videoconvert to make sure a superset of raw formats are supported
        vidconvsrc = Gst.ElementFactory.make("videoconvert", "convertor_src1")
        if not vidconvsrc:
            sys.stderr.write(" Unable to create videoconvert")

        # nvvideoconvert to convert incoming raw buffers to NVMM Mem (NvBufSurface API)
        nvvidconvsrc = Gst.ElementFactory.make("nvvideoconvert", "convertor_src2")
        if not nvvidconvsrc:
            sys.stderr.write(" Unable to create Nvvideoconvert")

        caps_vidconvsrc = Gst.ElementFactory.make("capsfilter", "nvmm_caps")
        if not caps_vidconvsrc:
            sys.stderr.write(" Unable to create capsfilter")

        # Create nvstreammux instance to form batches from one or more sources.
        streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
        if not streammux:
            sys.stderr.write(" Unable to create NvStreamMux")

        # Use nvinfer to run inferencing on camera's output,
        # behaviour of inferencing is set through config file
        pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")
        if not pgie:
            sys.stderr.write(" Unable to create pgie")

        # Use convertor to convert from NV12 to RGBA as required by nvosd
        nvvidconv = Gst.ElementFactory.make("nvvideoconvert", "convertor")
        if not nvvidconv:
            sys.stderr.write(" Unable to create nvvidconv")

        # Create OSD to draw on the converted RGBA buffer
        nvosd = Gst.ElementFactory.make("nvdsosd", "onscreendisplay")

        if not nvosd:
            sys.stderr.write(" Unable to create nvosd")

        # Finally render the osd output
        if is_aarch64():
            transform = Gst.ElementFactory.make("nvegltransform", "nvegl-transform")

        print("Creating EGLSink")
        sink = Gst.ElementFactory.make("nveglglessink", "nvvideo-renderer") # FIXME respect headless= here
        if not sink:
            sys.stderr.write(" Unable to create egl sink")

        print(f"Playing cam {sourcedev}")
        caps_v4l2src.set_property('caps', Gst.Caps.from_string("video/x-raw, framerate=30/1"))
        caps_vidconvsrc.set_property('caps', Gst.Caps.from_string("video/x-raw(memory:NVMM)"))
        source.set_property('device', sourcedev)
        streammux.set_property('width', 1920)
        streammux.set_property('height', 1080)
        streammux.set_property('batch-size', 1)
        streammux.set_property('batched-push-timeout', 4000000)
        pgie.set_property('config-file-path', "dstest1_pgie_config.txt")
        # Set sync = false to avoid late frame drops at the display-sink
        sink.set_property('sync', False)

        print("Adding elements to Pipeline")
        self.pipeline.add(source)
        self.pipeline.add(caps_v4l2src)
        self.pipeline.add(vidconvsrc)
        self.pipeline.add(nvvidconvsrc)
        self.pipeline.add(caps_vidconvsrc)
        self.pipeline.add(streammux)
        self.pipeline.add(pgie)
        self.pipeline.add(nvvidconv)
        self.pipeline.add(nvosd)
        self.pipeline.add(sink)
        if is_aarch64():
            self.pipeline.add(transform)

        # we link the elements together
        # v4l2src -> nvvideoconvert -> mux -> 
        # nvinfer -> nvvideoconvert -> nvosd -> video-renderer
        print("Linking elements in the Pipeline")
        source.link(caps_v4l2src)
        caps_v4l2src.link(vidconvsrc)
        vidconvsrc.link(nvvidconvsrc)
        nvvidconvsrc.link(caps_vidconvsrc)

        sinkpad = streammux.get_request_pad("sink_0")
        if not sinkpad:
            sys.stderr.write(" Unable to get the sink pad of streammux")
        srcpad = caps_vidconvsrc.get_static_pad("src")
        if not srcpad:
            sys.stderr.write(" Unable to get source pad of caps_vidconvsrc")
        srcpad.link(sinkpad)
        streammux.link(pgie)
        pgie.link(nvvidconv)
        nvvidconv.link(nvosd)
        if is_aarch64():
            nvosd.link(transform)
            transform.link(sink)
        else:
            nvosd.link(sink)

        # create an event loop and feed gstreamer bus mesages to it
        self.loop = GObject.MainLoop()
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect ("message", bus_call, self.loop)

        # Lets add probe to get informed of the meta data generated, we add probe to
        # the sink pad of the osd element, since by that time, the buffer would have
        # had got all the metadata.
        osdsinkpad = nvosd.get_static_pad("sink")
        if not osdsinkpad:
            sys.stderr.write(" Unable to get sink pad of nvosd")

        osdsinkpad.add_probe(Gst.PadProbeType.BUFFER, self.osd_sink_pad_buffer_probe, 0)

    def __enter__(self):
        self.running_list = []
        self.probing_yet = False
        # start play back and listen to events
        print("Starting pipeline")
        self.pipeline.set_state(Gst.State.PLAYING)
        print("Accessing loop method")
        target = self.loop.run
        print("Creating loop thread")
        self.input_thread = threading.Thread(target=target, name='DeepstreamLoop')
        print("Starting loop thread")
        self.input_thread.start()
        print("Started loop thread")
        while not self.probing_yet:
            print("Blocking until it actually starts probing")
            time.sleep(1)

    def __exit__(self, typ, val, tb):
        self.loop.quit()
        pipeline.set_state(Gst.State.NULL)
        self.input_thread.join()
    
    def settle_motion_detector(self):
        pass # Not needed for object detection

    def motions(self):
        stuff = self.running_list[:]
        del self.running_list[len(stuff):]
        return stuff

    def ignore_motion(self):
        pass # Not needed for object detection

    def resume(self):
        pass

if __name__ == '__main__':
    with DeepstreamPythonDetectorUnit(sys.argv[1]) as unit:
        while 1:
            time.sleep(10)

