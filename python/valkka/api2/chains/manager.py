"""
manager.py : Managed filterchain classes.  Resources are managed hierarchically, decoding is turned off if its not required

 * Copyright 2018 Valkka Security Ltd. and Sampsa Riikonen.
 * 
 * Authors: Sampsa Riikonen <sampsa.riikonen@iki.fi>
 * 
 * This file is part of the Valkka library.
 * 
 * Valkka is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 * 
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>
 *
 */

@file    manage.py
@author  Sampsa Riikonen
@date    2018
@version 0.5.3 
  
@brief   Managed filterchain classes.  Resources are managed hierarchically, decoding is turned off if its not required
"""

import sys
import time
import random
from valkka import valkka_core as core # so, everything that has .core, refers to the api1 level (i.e. swig wrapped cpp code)
from valkka.api2.threads import LiveThread, OpenGLThread # api2 versions of the thread classes
from valkka.api2.tools import parameterInitCheck, typeCheck
from valkka.api2.chains.port import ViewPort

pre_mod="valkka.api2.chains.manage : "


class ManagedFilterchain:
  """This class implements the following filterchain:
  
  ::                                                                                      +-->
                                                                                          |
    (LiveThread:livethread) -->> (AVThread:avthread) --> {ForkFrameFilterN:fork_filter} --+-->  .. OpenGLTreads, RenderContexts
                                                                                          |
                                                                                          +-->

  OpenGLThread(s) and stream connections to windows (RenderContexts) are created upon request.
  Decoding at AVThread is turned on/off, depending if it is required downstream

  """
  
  parameter_defs={
    "livethread"       : LiveThread,
    "openglthreads"    : list,
    "address"          : str,
    "slot"             : int,
    
    # these are for the AVThread instance:
    "n_basic"      : (int,20), # number of payload frames in the stack
    "n_setup"      : (int,20), # number of setup frames in the stack
    "n_signal"     : (int,20), # number of signal frames in the stack
    "flush_when_full" : (bool, False), # clear fifo at overflow
    
    "affinity"     : (int,-1),
    "verbose"      : (bool,False),
    "msreconnect"  : (int,0),
    
    "time_correction"   : None,    # Timestamp correction type: TimeCorrectionType_none, TimeCorrectionType_dummy, or TimeCorrectionType_smart (default)
    "recv_buffer_size"  : (int,0), # Operating system socket ringbuffer size in bytes # 0 means default
    "reordering_mstime" : (int,0)  # Reordering buffer time for Live555 packets in MILLIseconds # 0 means default
    }
  
  
  def __init__(self, **kwargs):
    self.pre=self.__class__.__name__+" : " # auxiliary string for debugging output
    parameterInitCheck(self.parameter_defs,kwargs,self) # check for input parameters, attach them to this instance as attributes
    for openglthread in self.openglthreads:
      assert(issubclass(openglthread.__class__,OpenGLThread))
    self.init()
    
    
  def init(self):
    self.idst=str(id(self))
    
    # init the manager
    self.ports =[]
    self.tokens_by_port={}
    
    self.makeChain()
    self.createContext()
    self.startThreads()
    self.active=True
    
    
  def __del__(self):
    self.close()
    
    
  def close(self):
    if (self.active):
      if (self.verbose):
        print(self.pre,"Closing threads and contexes")
      self.decodingOff()
      self.closeContext()
      self.stopThreads()
      self.active=False
    
    
  def makeChain(self):
    """Create the filter chain
    """
    self.fork_filter=core.ForkFrameFilterN("av_fork_at_slot_"+str(self.slot)) # FrameFilter chains can attached to ForkFrameFilterN after it's been instantiated
    
    self.framefifo_ctx=core.FrameFifoContext()
    self.framefifo_ctx.n_basic           =self.n_basic
    self.framefifo_ctx.n_setup           =self.n_setup
    self.framefifo_ctx.n_signal          =self.n_signal
    self.framefifo_ctx.flush_when_full   =self.flush_when_full
    
    self.avthread      =core.AVThread("avthread_"+self.idst, self.fork_filter, self.framefifo_ctx)
    self.avthread.setAffinity(self.affinity)
    self.av_in_filter  =self.avthread.getFrameFilter() # get input FrameFilter from AVThread

  
  """
  def connect(self,name,framefilter):
    return self.fork_filter.connect(name,framefilter)
    
    
  def disconnect(self,name):
    return self.fork_filter.disconnect(name)
  """
    

  def createContext(self):
    """Creates a LiveConnectionContext and registers it to LiveThread
    """
    # define stream source, how the stream is passed on, etc.
    
    self.ctx=core.LiveConnectionContext()
    self.ctx.slot=self.slot                          # slot number identifies the stream source
    
    if (self.address.find("rtsp://")==0):
      self.ctx.connection_type=core.LiveConnectionType_rtsp
    else:
      self.ctx.connection_type=core.LiveConnectionType_sdp # this is an rtsp connection
    
    self.ctx.address=self.address         
    # stream address, i.e. "rtsp://.."
    
    self.ctx.framefilter=self.av_in_filter
    
    self.ctx.msreconnect=self.msreconnect
    
    # some extra parameters
    """
    // ctx.time_correction =TimeCorrectionType::none;
    // ctx.time_correction =TimeCorrectionType::dummy;
    // default time correction is smart
    // ctx.recv_buffer_size=1024*1024*2;  // Operating system ringbuffer size for incoming socket
    // ctx.reordering_time =100000;       // Live555 packet reordering treshold time (microsecs)
    """
    if (self.time_correction!=None): self.ctx.time_correction =self.time_correction
    self.ctx.recv_buffer_size =self.recv_buffer_size
    self.ctx.reordering_time  =self.reordering_mstime*1000 # from millisecs to microsecs
    
    # send the information about the stream to LiveThread
    self.livethread.registerStream(self.ctx)
    self.livethread.playStream(self.ctx)

      
  def closeContext(self):
    self.livethread.stopStream(self.ctx)
    self.livethread.deregisterStream(self.ctx)
    
      
  def startThreads(self):
    """Starts thread required by the filter chain
    """
    self.avthread.startCall()


  def stopThreads(self):
    """Stops threads in the filter chain
    """
    self.avthread.stopCall()
    

  def decodingOff(self):
    self.avthread.decodingOffCall()


  def decodingOn(self):
    self.avthread.decodingOnCall()


  def addViewPort(self,view_port):
    assert(issubclass(view_port.__class__,ViewPort))
    # ViewPort object is created by the widget .. and stays alive while the widget exists.
    
    window_id      =view_port.getWindowId()
    x_screen_num   =view_port.getXScreenNum()
    openglthread   =self.openglthreads[x_screen_num]
    
    if (self.verbose):
      print(self.pre,"addViewPort: view_port, window_id, x_screen_num", view_port, window_id, x_screen_num)
    
    if (view_port in self.ports):
      # TODO: implement == etc. operators : compare window_id (and x_screen_num) .. nopes, if the object stays the same
      self.delViewPort(view_port)
  
    # run through all ViewPort instances in self.ports to find the number of x-screen requests
    n_x_screen_ports =self.getNumXscreenPorts(x_screen_num)
    
    if (n_x_screen_ports<1):
      # this only in the first time : start sending frames to X screen number x_screen_num!
      if (self.verbose):
        print(self.pre,"addViewPort: start streaming to x-screen", x_screen_num)
      self.fork_filter.connect("openglthread_"+str(x_screen_num),openglthread.getInput())
    
    token=openglthread.connect(slot=self.slot,window_id=window_id) # send frames from this slot to correct openglthread and window_id
    self.tokens_by_port[view_port]=token
    
    if (len(self.ports)<1):
      # first request for this stream : time to start decoding!
      if (self.verbose):
        print(self.pre,"addViewPort: start decoding slot",self.slot)
      self.avthread.decodingOnCall()
      
    self.ports.append(view_port)
      
    
  def delViewPort(self,view_port):
    assert(issubclass(view_port.__class__,ViewPort))
    
    window_id      =view_port.getWindowId()
    x_screen_num   =view_port.getXScreenNum()
    openglthread   =self.openglthreads[x_screen_num]
    
    if (self.verbose):
      print(self.pre,"delViewPort: view_port, window_id, x_screen_num", view_port, window_id, x_screen_num)
    
    if (view_port not in self.ports):
      print(self.pre,"delViewPort : FATAL : no such port", view_port)
      return
    
    self.ports.remove(view_port) # remove this port from the list
    token=self.tokens_by_port.pop(view_port) # remove the token associated to x-window output
    openglthread.disconnect(token) # stop the slot => render context / x-window mapping associated to the token
    
    n_x_screen_ports =self.getNumXscreenPorts(x_screen_num)
    
    if (n_x_screen_ports<1):
      # no need to send this stream to X Screen number x_screen_num
      if (self.verbose):
        print(self.pre,"delViewPort: removing stream from x-screen",x_screen_num)
      
      self.fork_filter.disconnect("openglthread_"+str(x_screen_num))
    
    if (len(self.ports)<1):
      # no need to decode the stream anymore
      self.avthread.decodingOffCall()
    
    
  def getNumXscreenPorts(self,x_screen_num):
    """Run through ViewPort's, count how many of them are using X screen number x_screen_num
    """
    sm=0
    for view_port in self.ports:
      if (issubclass(view_port.__class__,ViewPort)):
        if (view_port.getXScreenNum()==x_screen_num):
          sm+=1
    if (self.verbose):
      print(self.pre,"getNumXscreenPorts: slot",self.slot,"serves",sm+1,"view ports")
    return sm
        
    
def main():
  pre=pre_mod+"main :"
  print(pre,"main: arguments: ",sys.argv)
  if (len(sys.argv)<2):
    print(pre,"main: needs test number")
  else:
    st="test"+str(sys.argv[1])+"()"
    exec(st)
  
  
if (__name__=="__main__"):
  main()

"""
TODO:

next steps:

  - valkka-examples: 
    - a more interactive test_studio program: create a window on-demand.  The window has "change x screen" and "set stream" buttons.  "set stream" opens a drop-down list of cameras.
    - .. that'll be the last one of the simple test_studio programs: n x n grid views etc. and the gui management in general should be in a separate (proprietary) module


"""


