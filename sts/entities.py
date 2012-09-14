"""
This module mocks out openflow switches, links, and hosts. These are all the
'entities' that exist within our simulated environment.
"""

from pox.openflow.software_switch import SoftwareSwitch, DpPacketOut, ControllerConnection
from pox.openflow.nx_software_switch import NXSoftwareSwitch
from pox.lib.util import assert_type
from pox.openflow.libopenflow_01 import *
from pox.lib.revent import Event, EventMixin
from sts.procutils import popen_filtered, kill_procs
from sts.console import msg

import logging
import pickle
import signal

class CpMessageEvent (Event):
  """ Event raised when a control plane packet is sent or received """
  def __init__ (self, connections_used, message):
    assert_type("message", message, ofp_header, none_ok=False)
    Event.__init__(self)
    self.connections_used = connections_used
    self.message = message

class ShimHandler (object):
  '''
  Interpose on switch ofp receive handlers:
  pass CpMessageEvents to the ManagementPanel for fingerprinting purposes
  after invoking the original handler
  '''

  def __init__(self, original_handler, parent_switch):
    self.original_handler = original_handler
    self.parent_switch = parent_switch

  def __call__(self, *args, **kws):
    # Raise event /after/ the message has been processed.
    # This means that a BufferedPatchPanel /must/ be used!
    # Otherwise, the next line could cause a chain of DpPacketOut events
    # to occur before we have the opportunity to raise the CpMessageEvent
    self.original_handler(*args, **kws)

    # NXSoftwareSwitch "beefs up" the SoftwareSwitch receive handlers by
    # inserting the connection as the first param
    assert(type(args[0]) == ControllerConnection)
    assert(type(args[1]) == ofp_header)
    connections_used = [args[0]]
    msg = args[1]
    self.parent_switch.raiseEvent(CpMessageEvent(connections_used, msg))

class FuzzSoftwareSwitch (NXSoftwareSwitch):
  """
  A mock switch implementation for testing purposes. Can simulate dropping dead.
  """

  _eventMixin_events = set([DpPacketOut, CpMessageEvent])

  def __init__ (self, create_io_worker, dpid, name=None, ports=4, miss_send_len=128,
                n_buffers=100, n_tables=1, capabilities=None):
    NXSoftwareSwitch.__init__(self, dpid, name, ports, miss_send_len, n_buffers, n_tables, capabilities)
    # Overwrite the ofp receive handlers with a shim layer that passes
    # CpMessageEvents to the ManagementPanel for fingerprinting purposes
    original_handlers = self.ofp_handlers
    self.ofp_handlers = {
      key : ShimHandler(handler)
      for key, handler in original_handlers.iteritems()
    }
    # For messages initiated from switch -> controller, we override the
    # send() method below

    self.create_io_worker = create_io_worker

    self.failed = False
    self.log = logging.getLogger("FuzzSoftwareSwitch(%d)" % dpid)

    def error_handler(e):
      self.log.exception(e)
      raise e

    # controller (ip, port) -> connection
    self.uuid2connection = {}
    self.error_handler = error_handler
    self.controller_info = []

  # ------------------------------------------------- #
  # Override message sends from switch -> controller  #
  # ------------------------------------------------- #
  def send(self, message):
    # Log /after/ the send() has been put on the wire
    connections_used = super(FuzzSoftwareSwitch, self).send(message)
    self.raiseEvent(CpMessageEvent(connections_used, message))

  def add_controller_info(self, info):
    self.controller_info.append(info)

  def _handle_ConnectionUp(self, event):
    self._setConnection(event.connection, event.ofp)

  def connect(self):
    # NOTE: create_io_worker is /not/ an instancemethod but just a function
    # so we have to pass in the self parameter explicitly
    for info in self.controller_info:
      io_worker = self.create_io_worker(self, info)
      conn = self.set_io_worker(io_worker)
      # cause errors to be raised
      conn.error_handler = self.error_handler
      # controller (ip, port) -> connection
      self.uuid2connection[io_worker.socket.getpeername()] = conn

  def get_connection(self, uuid):
    if uuid not in self.uuid2connection:
      raise ValueError("No such connection %s" % str(uuid))
    return self.uuid2connection[uuid]

  def fail(self):
    # TODO(cs): depending on the type of failure, a real switch failure
    # might not lead to an immediate disconnect
    if self.failed:
      self.log.warn("Switch already failed")
      return
    self.failed = True

    for connection in self.connections:
      connection.close()
    self.connections = []

  def recover(self):
    if not self.failed:
      self.log.warn("Switch already up")
      return
    self.connect()
    self.failed = False

  def serialize(self):
    # Skip over non-serializable data, e.g. sockets
    # TODO(cs): is self.log going to be a problem?
    serializable = MockOpenFlowSwitch(self.dpid, self.parent_controller_name)
    # Can't serialize files
    serializable.log = None
    # TODO(cs): need a cleaner way to add in the NOM port representation
    if self.software_switch:
      serializable.ofp_phy_ports = self.software_switch.ports.values()
    return pickle.dumps(serializable, protocol=0)

class Link (object):
  """
  A network link between two switches

  Temporary stand in for Murphy's graph-library for the NOM.

  Note: Directed!
  """
  def __init__(self, start_software_switch, start_port, end_software_switch, end_port):
    if type(start_port) == int:
      assert(start_port in start_software_switch.ports)
      start_port = start_software_switch.ports[start_port]
    if type(end_port) == int:
      assert(end_port in start_software_switch.ports)
      end_port = end_software_switch.ports[end_port]
    assert_type("start_port", start_port, ofp_phy_port, none_ok=False)
    assert_type("end_port", end_port, ofp_phy_port, none_ok=False)
    self.start_software_switch = start_software_switch
    self.start_port = start_port
    self.end_software_switch = end_software_switch
    self.end_port = end_port

  def __eq__(self, other):
    if not type(other) == Link:
      return False
    return (self.start_software_switch == other.start_software_switch and
           self.start_port == other.start_port and
           self.end_software_switch == other.end_software_switch and
           self.end_port == other.end_port)

  def __hash__(self):
    return (self.start_software_switch.__hash__() +  self.start_port.__hash__() +
           self.end_software_switch.__hash__() +  self.end_port.__hash__())

  def __repr__(self):
    return "(%d:%d) -> (%d:%d)" % (self.start_software_switch.dpid, self.start_port.port_no,
                                   self.end_software_switch.dpid, self.end_port.port_no)

  def reversed_link(self):
    '''Create a Link that is in the opposite direction of this Link.'''
    return Link(self.end_software_switch, self.end_port,
                self.start_software_switch, self.start_port)

class AccessLink (object):
  '''
  Represents a bidirectional edge: host <-> ingress switch
  '''
  def __init__(self, host, interface, switch, switch_port):
    assert_type("interface", interface, HostInterface, none_ok=False)
    assert_type("switch_port", switch_port, ofp_phy_port, none_ok=False)
    self.host = host
    self.interface = interface
    self.switch = switch
    self.switch_port = switch_port

class HostInterface (object):
  ''' Represents a host's interface (e.g. eth0) '''
  def __init__(self, hw_addr, ip_or_ips=[], name=""):
    self.hw_addr = hw_addr
    if type(ip_or_ips) != list:
      ip_or_ips = [ip_or_ips]
    self.ips = ip_or_ips
    self.name = name

  def __eq__(self, other):
    if type(other) != HostInterface:
      return False
    if self.hw_addr.toInt() != other.hw_addr.toInt():
      return False
    other_ip_ints = map(lambda ip: ip.toUnsignedN(), other.ips)
    for ip in self.ips:
      if ip.toUnsignedN() not in other_ip_ints:
        return False
    if len(other.ips) != len(self.ips):
      return False
    if self.name != other.name:
      return False
    return True

  def __hash__(self):
    hash_code = self.hw_addr.toInt().__hash__()
    for ip in self.ips:
      hash_code += ip.toUnsignedN().__hash__()
    hash_code += self.name.__hash__()
    return hash_code

  def __str__(self, *args, **kwargs):
    return "HostInterface:" + self.name + ":" + str(self.hw_addr) + ":" + str(self.ips)

  def __repr__(self, *args, **kwargs):
    return self.__str__()

#                Host
#          /      |       \
#  interface   interface  interface
#    |            |           |
# access_link acccess_link access_link
#    |            |           |
# switch_port  switch_port  switch_port

class Host (EventMixin):
  '''
  A very simple Host entity.

  For more sophisticated hosts, we should spawn a separate VM!

  If multiple host VMs are too heavy-weight for a single machine, run the
  hosts on their own machines!
  '''
  _eventMixin_events = set([DpPacketOut])

  def __init__(self, interfaces, name=""):
    '''
    - interfaces A list of HostInterfaces
    '''
    self.interfaces = interfaces
    self.log = logging.getLogger(name)
    self.name = name

  def send(self, interface, packet):
    ''' Send a packet out a given interface '''
    self.log.info("sending packet on interface %s: %s" % (interface.name, str(packet)))
    self.raiseEvent(DpPacketOut(self, packet, interface))

  def receive(self, interface, packet):
    '''
    Process an incoming packet from a switch

    Called by PatchPanel
    '''
    self.log.info("received packet on interface %s: %s" % (interface.name, str(packet)))

  def __str__(self):
    return self.name

class Controller(object):
  '''Encapsulates the state of a running controller.'''

  _active_processes = set() # set of processes that are currently running. These are all killed upon signal reception

  @staticmethod
  def kill_active_procs():
    '''Kill the active processes. Used by the simulator module to shut down the
    controllers because python can only have a single method to handle SIG* stuff.'''
    kill_procs(Controller._active_processes)

  def _register_proc(self, proc):
    '''Register a Popen instance that a controller is running in for the cleanup
    that happens when the simulator receives a signal. This method is idempotent.'''
    self._active_processes.add(proc)

  def _unregister_proc(self, proc):
    '''Remove a process from the set of this to be killed when a signal is
    received. This is for use when the Controller process is stopped. This
    method is idempotent.'''
    self._active_processes.discard(proc)

  def __del__(self):
    if hasattr(self, 'process') and self.process != None: # if it fails in __init__, process may not have been assigned
      if self.process.poll():
        self._unregister_proc(self.process) # don't let this happen for shutdown
      else:
        self.kill() # make sure it is killed if this was started errantly

  def __init__(self, controller_config):
    '''idx is the unique index for the controller used mostly for logging purposes.'''
    self.config = controller_config
    self.alive = False
    self.process = None

  @property
  def pid(self):
    '''Return the PID of the Popen instance the controller was started with.'''
    return self.process.pid if self.process else None

  @property
  def uuid(self):
    '''Return the uuid of this controller. See ControllerConfig for more details.'''
    return self.config.uuid

  def kill(self):
    '''Kill the process the controller is running in.'''
    msg.event("Killing controller %s" % (str(self.uuid)))
    kill_procs([self.process])
    self._unregister_proc(self.process)
    self.alive = False
    self.process = None

  def start(self):
    '''Start a new controller process based on the config's cmdline
    attribute. Registers the Popen member variable for deletion upon a SIG*
    received in the simulator process.'''
    msg.event("Starting controller %s" % (str(self.uuid)))
    self.process = popen_filtered("c%s" % str(self.uuid), self.config.expanded_cmdline, self.config.cwd)
    self._register_proc(self.process)

    self.alive = True

  def restart(self):
    self.kill()
    self.start()

  def send_policy_request(self, controller, api_call):
    pass

