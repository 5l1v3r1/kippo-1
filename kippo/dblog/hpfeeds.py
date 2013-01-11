from kippo.core import dblog
from twisted.python import log

import os
import struct
import hashlib
import json
import socket
import uuid
import time
import hashlib
import thread
import base64

BUFSIZE = 16384
PUBMAXSIZE = 5*(1024**2)

OP_ERROR        = 0
OP_INFO         = 1
OP_AUTH         = 2
OP_PUBLISH      = 3
OP_SUBSCRIBE    = 4

MAXBUF = (1024**2)+PUBMAXSIZE
SIZES = {
	OP_ERROR: 5+MAXBUF,
	OP_INFO: 5+256+20,
	OP_AUTH: 5+256+20,
	OP_PUBLISH: 5+MAXBUF,
	OP_SUBSCRIBE: 5+256*2,
}

KIPPOCHAN = 'kippo.sessions'
UNIQUECHAN = 'kippo.malware'
class BadClient(Exception):
        pass

# packs a string with 1 byte length field
def strpack8(x):
	if isinstance(x, str): x = x.encode('latin1')
	return struct.pack('!B', len(x)) + x

# unpacks a string with 1 byte length field
def strunpack8(x):
	l = x[0]
	return x[1:1+l], x[1+l:]
	
def msghdr(op, data):
	return struct.pack('!iB', 5+len(data), op) + data
def msgpublish(ident, chan, data):
	return msghdr(OP_PUBLISH, strpack8(ident) + strpack8(chan) + data)
def msgsubscribe(ident, chan):
	if isinstance(chan, str): chan = chan.encode('latin1')
	return msghdr(OP_SUBSCRIBE, strpack8(ident) + chan)
def msgauth(rand, ident, secret):
	hash = hashlib.sha1(bytes(rand)+secret).digest()
	return msghdr(OP_AUTH, strpack8(ident) + hash)

class FeedUnpack(object):
	def __init__(self):
		self.buf = bytearray()
	def __iter__(self):
		return self
	def next(self):
		return self.unpack()
	def feed(self, data):
		self.buf.extend(data)
	def unpack(self):
		if len(self.buf) < 5:
			raise StopIteration('No message.')

		ml, opcode = struct.unpack('!iB', buffer(self.buf,0,5))
		if ml > SIZES.get(opcode, MAXBUF):
			raise BadClient('Not respecting MAXBUF.')

		if len(self.buf) < ml:
			raise StopIteration('No message.')

		data = bytearray(buffer(self.buf, 5, ml-5))
		del self.buf[:ml]
		return opcode, data

class hpclient(object):
	def __init__(self, server, port, ident, secret, debug):
		print 'hpfeeds client init broker {0}:{1}, identifier {2}'.format(server, port, ident)
		self.server, self.port = server, int(port)
		self.ident, self.secret = ident.encode('latin1'), secret.encode('latin1')
		self.debug = debug
		self.unpacker = FeedUnpack()
		self.state = 'INIT'

		self.connect()
		self.sendfiles = []
		self.filehandle = None

	def connect(self):
		self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self.s.settimeout(3)
		try: self.s.connect((self.server, self.port))
		except:
			print 'hpfeeds client could not connect to broker.'
			self.s = None
		else:
			self.s.settimeout(None)
			self.handle_established()

	def send(self, data):
		if not self.s: return
		self.s.send(data)

	def close(self):
		self.s.close()
		self.s = None

	def handle_established(self):
		if self.debug: print 'hpclient established'
		while self.state != 'GOTINFO':
			self.read()

		#quickly try to see if there was an error message
		self.s.settimeout(0.5)
		self.read()
		self.s.settimeout(None)
		
	def handle_io_in(self, indata):
		self.unpacker.feed(indata)

		# if we are currently streaming a file, delay handling incoming messages
		if self.filehandle:
			return len(indata)

		try:
			for opcode, data in self.unpacker:
				logger.debug('hpclient msg opcode {0} data {1}'.format(opcode, data))
				if opcode == OP_INFO:
					name, rand = strunpack8(data)
					logger.debug('hpclient server name {0} rand {1}'.format(name, rand))
					self.send(msgauth(rand, self.ident, self.secret))

				elif opcode == OP_PUBLISH:
					ident, data = strunpack8(data)
					chan, data = strunpack8(data)
					logger.debug('publish to {0} by {1}: {2}'.format(chan, ident, data))

				elif opcode == OP_ERROR:
					logger.debug('errormessage from server: {0}'.format(data))
				else:
					logger.debug('unknown opcode message: {0}'.format(opcode))
		except BadClient:
			logger.critical('unpacker error, disconnecting.')
			self.close()

		return len(indata)
		
	def read(self):
		if not self.s: return
		try: d = self.s.recv(BUFSIZE)
		except socket.timeout:
			return

		if not d:
			if self.debug: log.msg('hpclient connection closed?')
			self.close()
			return

		self.unpacker.feed(d)
		try:
			for opcode, data in self.unpacker:
				if self.debug: log.msg('hpclient msg opcode {0} data {1}'.format(opcode, data))
				if opcode == OP_INFO:
					name, rand = strunpack8(data)
					if self.debug: log.msg('hpclient server name {0} rand {1}'.format(name, rand))
					self.send(msgauth(rand, self.ident, self.secret))
					self.state = 'GOTINFO'

				elif opcode == OP_PUBLISH:
					ident, data = strunpack8(data)
					chan, data = strunpack8(data)
					if self.debug: log.msg('publish to {0} by {1}: {2}'.format(chan, ident, data))

				elif opcode == OP_ERROR:
					log.err('errormessage from server: {0}'.format(data))
				else:
					log.err('unknown opcode message: {0}'.format(opcode))
		except BadClient:
			log.err('unpacker error, disconnecting.')
			self.close()

	def publish(self, channel, **kwargs):
		self.send(msgpublish(self.ident, channel, json.dumps(kwargs).encode('latin1')))

	def sendfile(self, filepath):
		# does not read complete binary into memory, read and send chunks
		if not self.filehandle:
			self.sendfileheader(filepath)
			self.sendfiledata()
			self.filehandle = None
		else: 
			self.sendfiles.append(filepath)

	def sendfileheader(self, filepath):
		self.filehandle = open(filepath, 'rb')
		fsize = os.stat(filepath).st_size
		if( fsize > PUBMAXSIZE):
			fsize = PUBMAXSIZE
		headc = strpack8(self.ident) + strpack8(UNIQUECHAN)
		headh = struct.pack('!iB', 5+len(headc)+fsize, OP_PUBLISH)
		self.send(headh + headc)

	def sendfiledata(self):
		tmp = self.filehandle.read(PUBMAXSIZE)
		if not tmp:
			if self.sendfiles:
				fp = self.sendfiles.pop(0)
				self.sendfileheader(fp)
			else:
				self.filehandle = None
				self.handle_io_in(b'')
		else:
			self.send(tmp)


class DBLogger(dblog.DBLogger):
	def start(self, cfg):
		print 'hpfeeds DBLogger start'

		server	= cfg.get('database_hpfeeds', 'server')
		port	= cfg.get('database_hpfeeds', 'port')
		ident	= cfg.get('database_hpfeeds', 'identifier')
		secret	= cfg.get('database_hpfeeds', 'secret')
		debug	= cfg.get('database_hpfeeds', 'debug')

		self.client = hpclient(server, port, ident, secret, debug)
		self.meta = {}
		self.malwarePath = []

	# We have to return an unique ID
	def createSession(self, peerIP, peerPort, hostIP, hostPort):
		self.malwarePath = []
		session = uuid.uuid4().hex
		self.meta[session] = {'peerIP': peerIP, 'peerPort': peerPort, 
		'hostIP': hostIP, 'hostPort': hostPort, 'loggedin': None,
		'credentials':[], 'version': None, 'ttylog': None, 'command':[], 'malware':[], }
		return session
	def sendfile(self, malwarePath):
		if os.path.exists(malwarePath):
			#originfile = open(malwarePath,'rb')
			#encodefilename =malwarePath.strip()+".hex"
			#encodefile = open(encodefilename,'wb')
			#encodefile.write( base64.b64encode(originfile.read().encode('hex')))
			#originfile.close()
			#encodefile.close()
			self.client.sendfile(malwarePath)
		else:
			print malwarePath+"does not exists"
	def handleConnectionLost(self, session, args):
		log.msg('publishing metadata to hpfeeds')
		meta = self.meta[session]
		ttylog = self.ttylog(session)
		if ttylog: meta['ttylog'] = ttylog.encode('hex')
		if self.malwarePath:
			for part in self.malwarePath:
				malwarePath, malwareUrl = part[0], part[1]
				f = file(malwarePath,'rb')
				hashsha1 = hashlib.sha1()
				hashsha1.update(f.read(PUBMAXSIZE))
				malwareSha1 = hashsha1.hexdigest()
				malwareMd5 = hashlib.md5(f.read(PUBMAXSIZE)).hexdigest()
				self.meta[session]['malware'].append((malwareUrl,malwareSha1,malwareMd5))
				f.close()
				print "sending file %s"%malwarePath
				thread.start_new_thread(self.sendfile,(malwarePath,))
		self.client.publish(KIPPOCHAN, **meta)

	def handleLoginFailed(self, session, args):
		u, p = args['username'], args['password']
		self.meta[session]['credentials'].append((u,p))

	def handleLoginSucceeded(self, session, args):
		u, p = args['username'], args['password']
		self.meta[session]['loggedin'] = (u,p)

	def handleCommand(self, session, args):
	    cmd, curtime, success = args['input'], time.strftime('%Y%m%d%H%M%S'), True
	    self.meta[session]['command'].append((cmd,curtime,success))

	def handleUnknownCommand(self, session, args):
	    cmd, curtime, success = args['input'], time.strftime('%Y%m%d%H%M%S'), False
	    self.meta[session]['command'].append((cmd,curtime,success))

	def handleInput(self, session, args):
		pass

	def handleTerminalSize(self, session, args):
		pass

	def handleClientVersion(self, session, args):
		v = args['version']
		self.meta[session]['version'] = v
	
	def handleWgetMalware(self, session, args):
		malwarePath, malwareUrl = args['malwarePath'], args['malwareUrl']
		self.malwarePath.append((malwarePath,malwareUrl))
		print "%s-%s"%(malwareUrl,malwarePath)
      
# vim: set sw=4 :et:
