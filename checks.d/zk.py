'''
As of ZooKeeper 3.4.0, the `mntr` admin command is provided for easy parsing of ZooKeeper stats.
This check first parses the `stat` command for a version number.
If the version supports `mntr`, `mntr` is also parsed.

Duplicate information is being reported by both `mntr` and `stat` to keep backwards compatability.
Example:
    `stat` reports: zookeeper.latency.avg
    `mntr` reports: zookeeper.avg.latency
You should make use of the stat reported by `mntr`.
The `stat` name is only kept for backward compatability reasons.

Parses the response from zookeeper's `stat` admin command, which looks like:

```
Zookeeper version: 3.2.2--1, built on 03/16/2010 07:31 GMT
Clients:
 /10.42.114.160:32634[1](queued=0,recved=12,sent=0)
 /10.37.137.74:21873[1](queued=0,recved=53613,sent=0)
 /10.37.137.74:21876[1](queued=0,recved=57436,sent=0)
 /10.115.77.32:32990[1](queued=0,recved=16,sent=0)
 /10.37.137.74:21891[1](queued=0,recved=55011,sent=0)
 /10.37.137.74:21797[1](queued=0,recved=19431,sent=0)

Latency min/avg/max: -10/0/20007
Received: 101032173
Sent: 0
Outstanding: 0
Zxid: 0x1034799c7
Mode: leader
Node count: 487
```

`stat` tested with Zookeeper versions 3.0.0 to 3.4.5

The following is an example of the `mntr` commands output:

````
zk_version  3.4.5-cdh4.4.0--1, built on 09/04/2013 01:46 GMT
zk_avg_latency  0
zk_max_latency  0
zk_min_latency  0
zk_packets_received 4
zk_packets_sent 3
zk_num_alive_connections    1
zk_outstanding_requests 0
zk_server_state standalone
zk_znode_count  4
zk_watch_count  0
zk_ephemerals_count 0
zk_approximate_data_size    27
zk_open_file_descriptor_count   29
zk_max_file_descriptor_count    4096
````

ZooKeeper `mntr` command may also output the following error:

````
This ZooKeeper instance is not currently serving requests
````

Stats parsed from `mntr` are reported with the given name
where 'zk' is replaced with 'zookeeper' and '_' is replaced with '.'
example: 'zk_avg_latency' becomes 'zookeeper.avg.latency'

The state of ZooKeeper is reported with the tag 'mode:{inactive,leader,standalone,follower,observer,unknown,down}'
'inactive' state is reported when `mntr` reports error.
`down` state is reported when zookeeper is unreachable.
`unknown` state is reported when any other exception occurs in this Check.
State and hostname are also reported through the set 'zookeeper.instances'

`mntr` tested with ZooKeeper 3.4.5
'''
# stdlib
import re
import socket
import sys
from StringIO import StringIO
import struct
from distutils.version import LooseVersion

# project
from checks import AgentCheck


class ZKConnectionFailure(Exception):
    """ Raised when we are unable to connect or get the output of a command. """
    pass


class ZookeeperCheck(AgentCheck):
    version_pattern = re.compile(r'Zookeeper version: ([^.]+)\.([^.]+)\.([^-]+)', flags=re.I)

    SOURCE_TYPE_NAME = 'zookeeper'

    def check(self, instance):
        host = instance.get('host', 'localhost')
        port = int(instance.get('port', 2181))
        timeout = float(instance.get('timeout', 3.0))
        expected_mode = (instance.get('expected_mode') or '').strip()
        tags = instance.get('tags', [])
        cx_args = (host, port, timeout)
        sc_tags = ["host:{0}".format(host), "port:{0}".format(port)]
        hostname = socket.gethostname()

        zk_version = None # parse_stat will parse and set version string

        # Send a service check based on the `ruok` response.
        try:
            ruok_out = self._send_command('ruok', *cx_args)
        except ZKConnectionFailure:
            # The server should not respond at all if it's not OK.
            status = AgentCheck.CRITICAL
            message = 'No response from `ruok` command'
            self.increment('zookeeper.timeouts')
            raise
        else:
            ruok_out.seek(0)
            ruok = ruok_out.readline()
            if ruok == 'imok':
                status = AgentCheck.OK
            else:
                status = AgentCheck.WARNING
            message = u'Response from the server: %s' % ruok
        finally:
            self.service_check('zookeeper.ruok', status, message=message,
                    tags=sc_tags)

        # Read metrics from the `stat` output.
        try:
            stat_out = self._send_command('stat', *cx_args)
        except ZKConnectionFailure:
            self.increment('zookeeper.timeouts')
            self.set_instance_status(hostname, 'down')
        except:
            e = sys.exc_info()[1]
            print >> sys.stderr, "Error: %s" % e
            self.increment('zookeeper.datadog_client_exception')
            self.set_instance_status(hostname, 'unknown')
        else:
            # Parse the response
            metrics, new_tags, state, zk_version = self.parse_stat(stat_out)
            mode = "mode:%s" % state

            # Write the data
            if state != 'inactive':
                for metric, value in metrics:
                    self.gauge(metric, value, tags=tags + new_tags)
            self.set_instance_status(hostname, state)

            if expected_mode:
                if state == expected_mode:
                    status = AgentCheck.OK
                    message = u"Server is in %s mode" % mode
                else:
                    status = AgentCheck.CRITICAL
                    message = u"Server is in %s mode but check expects %s mode"\
                              % (mode, expected_mode)
                self.service_check('zookeeper.mode', status, message=message,
                                   tags=sc_tags)

        if zk_version and LooseVersion(zk_version) > LooseVersion("3.4.0"):
            try:
                mntr_out = self._send_command('mntr', *cx_args)
            except ZKConnectionFailure:
                self.increment('zookeeper.timeouts')
            except:
                e = sys.exc_info()[1]
                print >> sys.stderr, "Error: %s" % e
                self.increment('zookeeper.datadog_client_exception')
            else:
                metrics, state = self.parse_mntr(mntr_out)
                mode = "mode:%s" % state
                if state != 'inactive':
                    for name in metrics:
                        self.gauge(name, metrics[name], tags=tags + [mode])


    def set_instance_status(self, hostname, status):
        mode = 'mode:%s' % status
        tags = [mode]
        self.set('zookeeper.instances', hostname, tags=tags)

        gauges = {
            'leader': 0,
            'follower': 0,
            'observer': 0,
            'standalone': 0,
            'down': 0,
            'inactive': 0,
            'unknown': 0
        }

        if status in gauges.keys():
            gauges[status] = 1
        else:
            gauges['unknown'] = 1

        for k in gauges:
            gauge_name = 'zookeeper.instances.%s' % k
            self.gauge(gauge_name, gauges[k])

    def _send_command(self, command, host, port, timeout):
        sock = socket.socket()
        sock.settimeout(timeout)
        buf = StringIO()
        chunk_size = 1024
        # try-finally and try-except to stay compatible with python 2.4
        try:
            try:
                # Connect to the zk client port and send the stat command
                sock.connect((host, port))
                sock.sendall(command)

                # Read the response into a StringIO buffer
                chunk = sock.recv(chunk_size)
                buf.write(chunk)
                num_reads = 1
                max_reads = 10000
                while chunk:
                    if num_reads > max_reads:
                        # Safeguard against an infinite loop
                        raise Exception("Read %s bytes before exceeding max reads of %s. "
                                        % (buf.tell(), max_reads))
                    chunk = sock.recv(chunk_size)
                    buf.write(chunk)
                    num_reads += 1
            except (socket.timeout, socket.error):
                raise ZKConnectionFailure()
        finally:
            sock.close()
        return buf


    def parse_stat(self, buf):
        ''' `buf` is a readable file-like object
            returns a tuple: (metrics, tags, mode, version)
        '''
        metrics = []
        buf.seek(0)

        # Check the version line to make sure we parse the rest of the
        # body correctly. Particularly, the Connections val was added in
        # >= 3.4.4.
        start_line = buf.readline()
        match = self.version_pattern.match(start_line)
        if match is None:
            return (None, None, "inactive", None)
            raise Exception("Could not parse version from stat command output: %s" % start_line)
        else:
            version_tuple = match.groups()
        has_connections_val = version_tuple >= ('3', '4', '4')
        version = "%s.%s.%s" % version_tuple

        # Clients:
        buf.readline() # skip the Clients: header
        connections = 0
        client_line = buf.readline().strip()
        if client_line:
            connections += 1
        while client_line:
            client_line = buf.readline().strip()
            if client_line:
                connections += 1

        # Latency min/avg/max: -10/0/20007
        _, value = buf.readline().split(':')
        l_min, l_avg, l_max = [int(v) for v in value.strip().split('/')]
        metrics.append(('zookeeper.latency.min', l_min))
        metrics.append(('zookeeper.latency.avg', l_avg))
        metrics.append(('zookeeper.latency.max', l_max))

        # Received: 101032173
        _, value = buf.readline().split(':')
        metrics.append(('zookeeper.bytes_received', long(value.strip())))

        # Sent: 1324
        _, value = buf.readline().split(':')
        metrics.append(('zookeeper.bytes_sent', long(value.strip())))

        if has_connections_val:
            # Connections: 1
            _, value = buf.readline().split(':')
            metrics.append(('zookeeper.connections', int(value.strip())))
        else:
            # If the zk version doesnt explicitly give the Connections val,
            # use the value we computed from the client list.
            metrics.append(('zookeeper.connections', connections))

        # Outstanding: 0
        _, value = buf.readline().split(':')
        # Fixme: This metric name is wrong. It should be removed in a major version of the agent
        # See https://github.com/DataDog/dd-agent/issues/1383
        metrics.append(('zookeeper.bytes_outstanding', long(value.strip())))
        metrics.append(('zookeeper.outstanding_requests', long(value.strip())))

        # Zxid: 0x1034799c7
        _, value = buf.readline().split(':')
        # Parse as a 64 bit hex int
        zxid = long(value.strip(), 16)
        # convert to bytes
        zxid_bytes = struct.pack('>q', zxid)
        # the higher order 4 bytes is the epoch
        (zxid_epoch,) = struct.unpack('>i', zxid_bytes[0:4])
        # the lower order 4 bytes is the count
        (zxid_count,) = struct.unpack('>i', zxid_bytes[4:8])

        metrics.append(('zookeeper.zxid.epoch', zxid_epoch))
        metrics.append(('zookeeper.zxid.count', zxid_count))

        # Mode: leader
        _, value = buf.readline().split(':')
        mode = value.strip().lower()
        tags = [u'mode:' + mode]

        # Node count: 487
        _, value = buf.readline().split(':')
        metrics.append(('zookeeper.nodes', long(value.strip())))

        return metrics, tags, mode, version


    def parse_mntr(self, buf):
        ''' `buf` is a readable file-like object
            returns a tuple: (metrics, state)
            if state == 'inactive', metrics will be None
        '''

        buf.seek(0)
        first = buf.readline() # first is version string or error
        if first == 'This ZooKeeper instance is not currently serving requests':
            return (None, 'inactive')

        metrics = {}

        for line in buf:
            data = line.split()
            if len(data) == 2:
                name = data[0].replace('zk', 'zookeeper').replace('_', '.')
                metrics[name] = data[1]
            else:
                raise Exception("Data not in 'key value' format, could not parse '%s'" % data.join(' '))

        # state is a string {'standalone', 'leader', 'follower', 'observer'}
        state = metrics.pop('zookeeper.server.state').lower()

        for key in metrics: # everything else is an int
            metrics[key] = int(metrics[key])

        return (metrics, state)
