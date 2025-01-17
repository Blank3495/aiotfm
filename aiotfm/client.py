import asyncio
import sys
import time
import traceback

from aiotfm.packet import Packet
from aiotfm.get_keys import get_keys
from aiotfm.connection import Connection
from aiotfm.player import Profile, Player
from aiotfm.tribe import Tribe
from aiotfm.message import Message, Whisper, Channel, ChannelMessage
from aiotfm.shop import Shop
from aiotfm.inventory import Inventory, InventoryItem, Trade
from aiotfm.room import Room
from aiotfm.locale import Locale
from aiotfm.errors import *

class Client:
	"""Represents a client that connects to Transformice.
	Two argument can be passed to the :class:`Client`.

	.. _event loop: https://docs.python.org/3/library/asyncio-eventloops.html

	Parameters
	----------
	community: Optional[:class:`int`]
		Defines the community of the client. Defaults to 0 (EN community).
	loop: Optional[event loop]
		The `event loop`_ to use for asynchronous operations. If ``None`` is passed (defaults),
		the event loop used will be ``asyncio.get_event_loop()``.

	Attributes
	----------
	username: Optional[:class:`str`]
		The bot's username received from the server. Might be None if the bot didn't log in yet.
	room: Optional[:class:`aiotfm.room.Room`]
		The bot's room. Might be None if the bot didn't log in yet or couldn't join any room yet.
	trade: Optional[:class:`aiotfm.inventory.Trade`]
		The current trade that's going on (i.e: both traders accepted it).
	trades: :class:`list`[:class:`aiotfm.inventory.Trade`]
		All the trades that the bot participates. Most of them might be invitations only.
	inventory: Optional[:class:`aiotfm.inventory.Inventory`]
		The bot's inventory. Might be None if the bot didn't log in yet or it didn't receive anything.
	locale: :class:`aiotfm.locale.Locale`
		The bot's locale (translations).
	"""
	LOG_UNHANDLED_PACKETS = False

	def __init__(self, community=0, loop=None):
		self.loop = loop or asyncio.get_event_loop()

		self.main = Connection('main', self, self.loop)
		self.bulle = None

		self._waiters = {}

		self.room = None
		self.trade = None
		self.trades = []
		self.inventory = None

		self.username = None
		self.locale = Locale()
		self.community = community # EN
		self.cp_fingerprint = 0

		self._channels = []

	async def received_data(self, data, connection):
		"""|coro|
		Dispatches the received data.

		:param data: :class:`bytes` the received data.
		:param connection: :class:`aiotfm.connection.Connection` the connection that received the data.
		"""
		self.dispatch('raw_socket', connection, Packet(data))
		try:
			await self.handle_packet(connection, Packet(data))
		except Exception:
			traceback.print_exc()

	async def handle_packet(self, connection:Connection, packet:Packet):
		"""|coro|
		Handles the known packets and dispatches events.
		Subclasses should handle only the unhandled packets from this method.

		Example: ::
			class Bot(aiotfm.Client):
				async def handle_packet(self, conn, packet):
					handled = await super().handle_packet(conn, packet.copy())

					if not handled:
						# Handle here the unhandled packets.
						pass

		:param connection: :class:`aiotfm.connection.Connection` the connection that received the packet.
		:param packet: :class:`aiotfm.Packet` the packet.
		:return: True if the packet got handled, False otherwise.
		"""
		CCC = packet.readCode()
		if CCC==(1, 1): # Old packets
			data = packet.readBytes().split(b'\x01')
			oldCCC = tuple(data.pop(0)[:2])
			self.dispatch('old_packet', connection, oldCCC, data)
			return await self.handle_old_packet(connection, oldCCC, data)

		elif CCC==(5, 21): # Joined room
			room = self.room = Room(private=not packet.readBool(), name=packet.readUTF())
			self.dispatch('joined_room', room)

		elif CCC==(6, 6): # Room message
			player_id = packet.read32()
			username = packet.readUTF()
			commu = packet.read8()
			message = packet.readUTF()
			self.dispatch('room_message', Message(Player(username, pid=player_id), message, commu, self))

		elif CCC==(6, 20): # Server message
			packet.readBool() # if False then the message will appear in the #Server channel
			t_key = packet.readUTF()
			t_args = [packet.readUTF() for i in range(packet.read8())]
			self.dispatch('server_message', self.locale[t_key], *t_args)

		elif CCC==(8, 5): # Show emoji
			player = self.room.get_player(pid=packet.read32())
			emoji = packet.read8()
			self.dispatch('emoji', player, emoji)

		elif CCC==(8, 16): # Profile
			self.dispatch('profile', Profile(packet))

		elif CCC==(8, 20): # Shop
			self.dispatch('shop', Shop(packet))

		elif CCC==(8, 22): # Skills
			skills = {}
			for i in range(packet.read8()):
				key, value = packet.read8(), packet.read8()
				skills[key] = value
			self.dispatch('skills', skills)

		elif CCC==(16, 2): # Tribe invitation received
			author = packet.readUTF()
			tribe = packet.readUTF()
			self.dispatch('tribe_inv', author, tribe)

		elif CCC==(26, 2): # Logged in successfully
			player_id = packet.read32()
			self.username = username = packet.readUTF()
			played_time = packet.read32()
			community = packet.read8()
			pid = packet.read32()
			self.dispatch('logged', player_id, username, played_time, community, pid)

		elif CCC==(26, 3): # Handshake OK
			online_players = packet.read32() # online players
			connection.fingerprint = packet.read8()
			community = packet.readUTF() # community
			country = packet.readUTF() # country
			self.authkey = packet.read32()

			self.loop.create_task(self._heartbeat_loop())

			await connection.send(Packet.new(8,2).write8(self.community).write8(0))

			os_info = Packet.new(28,17).writeString('en').writeString('Linux')
			os_info.writeString('LNX 29,0,0,140').write8(0)

			await connection.send(os_info)
			self.dispatch('login_ready', online_players, community, country)

		elif CCC==(26, 12): # Login result
			self.dispatch('login_result', packet.read8(), packet.readUTF(), packet.readUTF())

		elif CCC==(26, 25): # Ping
			self.dispatch('ping')

		elif CCC==(28, 6): # Server ping
			await connection.send(Packet.new(28, 6).write8(packet.read8()))

		elif CCC==(29, 6): # Lua logs
			self.dispatch('lua_log', packet.readUTF())

		elif CCC==(31, 1): # Inventory data
			self.inventory = Inventory.from_packet(packet)
			self.inventory.client = self
			self.dispatch('inventory_update', self.inventory)

		elif CCC==(31, 2): # Update inventory item
			id = packet.read16()
			quantity = packet.read8()

			if id in self.inventory.items:
				item = self.inventory.items[id]
				previous = item.quantity
				item.quantity = quantity
				self.dispatch('item_update', item, previous)

			else:
				item = InventoryItem(id=id, quantity=quantity)
				self.inventory.items[item.id] = item
				self.dispatch('new_item', item)

		elif CCC==(31, 5): # Trade invite
			player = self.room.get_player(pid=packet.read32())
			trade = Trade(player, self)
			self.trades.append(trade)
			trade.alive = True
			trade.on_invite = True
			self.dispatch('trade_invite', trade)

		elif CCC==(31, 6): # Trade error
			name = packet.readUTF()
			error = packet.read8()

			if name == "":
				if self.trade._other.username == name:
					self.trade._close()
					self.dispatch('trade_error', self.trade, error)
					self.dispatch('trade_close', self.trade)

			else:
				for trade in self.trades:
					if trade._other.username == name:
						trade._close()
						self.dispatch('trade_error', trade, error)
						self.dispatch('trade_close', trade)
						break

		elif CCC==(31, 7): # Trade start
			player = self.room.get_player(pid=packet.read32())
			player.trade.on_invite = False
			player.trade.alive = True

			if self.trade is not None:
				trade = self.trade
				self.trade._close()
				self.dispatch('trade_close', trade)
			self.trade = player.trade
			self.dispatch('trade_start', self.trade)

		elif CCC==(31, 8): # Trade items
			me = packet.readBool()
			id = packet.read16()
			adding = packet.readBool()
			quantity = packet.read8()
			quantity = (1 if adding else -1) * quantity

			items = self.trade.items_me if me else self.trade.items_other
			if id in items:
				items[id] += quantity
			else:
				items[id] = quantity
			if items[id] == 0:
				del items[id]

			self.trade.locked_me = False
			self.trade.locked_other = False

			self.dispatch('trade_item_change', self.trade, self if me else self.trade._other, id, quantity, items[id] if id in items else 0)

		elif CCC==(31, 9): # Trade lock
			if packet.readBool():
				self.trade.locked_me = packet.readBool()
				self.dispatch('trade_lock', self.trade, self, self.trade.locked_me)
			else:
				self.trade.locked_other = packet.readBool()
				self.dispatch('trade_lock', self.trade, self.trade._other, self.trade.locked_other)

		elif CCC==(31, 10): # Trade complete
			trade = self.trade
			self.trade._close()
			self.dispatch('trade_complete', trade)

		elif CCC==(44, 1): # Bulle switching
			bulle_id = packet.read32()
			bulle_ip = packet.readString().decode()

			if self.bulle is not None:
				self.bulle.close()

			self.bulle = Connection('bulle', self, self.loop)
			await self.bulle.connect(bulle_ip, self.main.address[1])
			await self.bulle.send(Packet.new(*CCC).write32(bulle_id))

		elif CCC==(44, 22): # Fingerprint offset changed
			connection.fingerprint = packet.read8()

		elif CCC==(60, 3): # Community platform
			TC = packet.read16()
			self.dispatch('raw_cp', TC, packet.copy(True))

			if TC==3: # Connected to the community platform
				self.dispatch('ready')

			elif TC==55: # Channel join result
				result = packet.read8()
				self.dispatch('channel_joined_result', result)

			elif TC==57: # Channel leave result
				result = packet.read8()
				self.dispatch('channel_leaved_result', result)

			elif TC==59: # Channel /who result
				idSequence = packet.read32()
				result = packet.read8()
				players = [Player(packet.readUTF()) for _ in range(packet.read16())]
				self.dispatch('channel_who', idSequence, players)

			elif TC==62: # Joined a channel
				name = packet.readUTF()

				if name in self._channels:
					channel = [c for c in self._channels if c==name][0]
				else:
					channel = Channel(name, self)
					self._channels.append(channel)

				self.dispatch('channel_joined', channel)

			elif TC==63: # Quit a channel
				name = packet.readUTF()
				if name in self._channels:
					self._channels.remove(name)

				self.dispatch('channel_closed', name)

			elif TC==64: # Channel message
				author, community = packet.readUTF(), packet.read32()
				channel_name, message = packet.readUTF(), packet.readUTF()
				channel = self.get_channel(channel_name)

				if channel is None:
					channel = Channel(channel_name, self)
					self._channels.append(channel)

				self.dispatch('channel_message', ChannelMessage(author, community, message, channel))

			elif TC==65: # Tribe message
				author, message = packet.readUTF(), packet.readUTF()
				self.dispatch('tribe_message', author, message)

			elif TC==66: # Whisper
				author, commu, receiver, message = Player(packet.readUTF()), packet.read32(), Player(packet.readUTF()), packet.readUTF()
				self.dispatch('whisper', Whisper(author, commu, receiver, message, self))

			elif TC==88: # tribe member connected
				self.dispatch('member_connected', packet.readUTF())

			elif TC==90: # tribe member disconnected
				self.dispatch('member_disconnected', packet.readUTF())

			else:
				if self.LOG_UNHANDLED_PACKETS:
					print(CCC, TC, bytes(packet.buffer)[4:])
				return False

		elif CCC==(100, 67): # New inventory item
			slot = packet.read8()
			id = packet.read16()
			quantity = packet.read8()

			item = InventoryItem(id=id, quantity=quantity, slot=None if slot == 0 else slot)
			self.inventory[id] = item
			self.dispatch('new_item', item)

		elif CCC==(144, 1): # Set player list
			before = self.room.players
			self.room.players = []

			for player in range(packet.read16()):
				self.room.players.append(Player.from_packet(packet))

			for player in before:
				if player.trade is not None:
					after = self.room.get_player(pid=player.pid)

					if after is not None:
						player.trade._update_player(after)
					else:
						trade = player.trade
						player.trade._close()
						self.dispatch('trade_close', trade)

			self.dispatch('bulk_player_update', before, self.room.players)

		elif CCC==(144, 2): # Add a player
			after = Player.from_packet(packet)
			before = self.room.get_player(pid=after.pid)

			self.room.players.append(after)
			if before is None:
				self.dispatch('player_join', after)
			else:
				self.room.players.remove(before)
				if before.trade is not None:
					before.trade._update_player(after)
				self.dispatch('player_update', before, after)

		else:
			if self.LOG_UNHANDLED_PACKETS:
				print(CCC, bytes(packet.buffer)[2:])
			return False

		return True

	async def handle_old_packet(self, connection:Connection, oldCCC:tuple, data:list):
		"""|coro|
		Handles the known packets from the old protocol and dispatches events.
		Subclasses should handle only the unhandled packets from this method.

		Example: ::
			class Bot(aiotfm.Client):
				async def handle_old_packet(self, conn, oldCCC, data):
					handled = await super().handle_old_packet(conn, data.copy())

					if not handled:
						# Handle here the unhandled packets.
						pass

		:param connection: :class:`aiotfm.connection.Connection` the connection that received the packet.
		:param oldCCC: :class:`tuple` the packet identifiers on the old protocol.
		:param data: :class:`list` the packet data.
		:return: True if the packet got handled, False otherwise.
		"""
		if oldCCC==(8, 7): # Remove a player
			player = self.room.get_player(pid=int(data[0]))

			if player is not None:
				self.room.players.remove(player)
				if player.trade is not None:
					trade = player[1].trade
					player.trade._close()
					self.dispatch('trade_close', trade)
				self.dispatch('player_remove', player)

		else:
			if self.LOG_UNHANDLED_PACKETS:
				print("[OLD]", oldCCC, data)
			return False

		return True

	async def _heartbeat_loop(self):
		"""|coro|
		Send a packet every fifteen seconds to stay connected to the game.
		"""
		last_heartbeat = 0
		while self.main.open:
			if self.loop.time()-last_heartbeat>=15:
				t = time.perf_counter()
				await self.main.send(Packet.new(26, 26))
				await self.main.send(Packet.new(26, 26))
				if self.bulle is not None and self.bulle.open:
					await self.bulle.send(Packet.new(26, 26))

				self.dispatch('heartbeat', (time.perf_counter()-t)*1000)
				last_heartbeat = self.loop.time()
			await asyncio.sleep(.5)

	def get_channel(self, name):
		if name is None:
			return None

		for channel in self._channels:
			if channel.name==name:
				return channel

	def event(self, coro):
		"""A decorator that registers an event.

		More about events [here](Events.md).
		"""
		name = coro.__name__
		if not name.startswith('on_'):
			raise InvalidEvent("'{}' isn't a correct event naming.".format(name))
		if not asyncio.iscoroutinefunction(coro):
			raise InvalidEvent("Couldn't register a non-coroutine function for the event {}.".format(name))

		setattr(self, name, coro)
		return coro

	def wait_for(self, event, condition=None, timeout=None, stopPropagation=False):
		"""Wait for an event.

		Example: ::
			@client.event
			async def on_room_message(author, message):
				if message=='id':
					await client.sendCommand('profile '+author)
					profile = await client.wait_for('on_profile', lambda p: p.username==author)
					await client.sendRoomMessage('Your id: {}'.format(profile.id))

		:param event: :class:`str` the event name.
		:param condition: Optionnal[:class:`function`] A predicate to check what to wait for. The arguments must meet the parameters of the event being waited for.
		:param timeout: Optionnal[:class:`int`] the number of seconds before raise asyncio.TimeoutError
		:return: :class:`asyncio.Future` a future that you must await.
		"""
		event = event.lower()
		future = self.loop.create_future()

		if condition is None:
			def condition(*a):
				return True

		if event not in self._waiters:
			self._waiters[event] = []

		self._waiters[event].append((condition, future, stopPropagation))

		return asyncio.wait_for(future, timeout, loop=self.loop)

	async def _run_event(self, coro, event_name, *args, **kwargs):
		"""|coro|
		Runs an event and handle the error if any.

		:param coro: a coroutine function.
		:param event_name: :class:`str` the event's name.
		:param args: arguments to pass to the coro.
		:param kwargs: keyword arguments to pass to the coro.
		"""
		try:
			await coro(*args, **kwargs)
		except asyncio.CancelledError:
			pass
		except Exception as e:
			if hasattr(self, 'on_error'):
				try:
					await self.on_error(event_name, e, *args, **kwargs)
				except asyncio.CancelledError:
					pass

	def dispatch(self, event, *args, **kwargs):
		"""Dispatches events

		:param event: :class:`str` event's name. (without 'on_')
		:param args: arguments to pass to the coro.
		:param kwargs: keyword arguments to pass to the coro.
		"""
		method = 'on_' + event

		if method in self._waiters:
			to_remove = []
			waiters = self._waiters[method]
			for i, (cond, fut, stop) in enumerate(waiters):
				if fut.cancelled():
					to_remove.append(i)
					continue

				try:
					result = bool(cond(*args))
				except Exception as e:
					fut.set_exception(e)
				else:
					if result:
						fut.set_result(args[0] if len(args)==1 else args if len(args) else None)
						if stop:
							del waiters[i]
							return
						else:
							to_remove.append(i)

			if len(to_remove)==len(waiters):
				del self._waiters[method]
			else:
				for i in to_remove[::-1]:
					del waiters[i]

		coro = getattr(self, method, None)
		if coro is not None:
			asyncio.ensure_future(self._run_event(coro, method, *args, **kwargs), loop=self.loop)

	async def on_error(self, event, err, *a, **kw):
		message = '\nAn error occurred while dispatching the event "{0}":\n\n{2}'
		tb = traceback.format_exc(limit=-1)
		print(message.format(event, err, tb), file=sys.stderr)
		return message.format(event, err, tb)

	async def on_connection_error(self, conn, error):
		print('{0.__class__.__name__}: {0}'.format(error), file=sys.stderr)

		if isinstance(error, EOFError):
			self.close()

	async def start(self, api_tfmid, api_token, keys=None):
		"""|coro|
		Connects the client to the game.

		:param api_tfmid: :class:`int` or :class:`str` your Transformice id.
		:param api_token: :class:`str` your token to access the API.
		"""
		if keys is not None:
			self.keys = keys
		else:
			self.keys = keys = await get_keys(api_tfmid, api_token)

		for port in [13801, 11801, 12801, 14801]:
			try:
				await self.main.connect('94.23.193.229', port)
			except:
				pass
			else:
				break
		else:
			raise ConnectionError('Unable to connect to the server.')

		while not self.main.socket.connected:
			await asyncio.sleep(.1)

		packet = Packet.new(28, 1).write16(keys.version).writeString(keys.connection)
		packet.writeString('Desktop').writeString('-').write32(0x1fbd).writeString('')
		packet.writeString('74696720697320676f6e6e61206b696c6c206d7920626f742e20736f20736164')
		packet.writeString("A=t&SA=t&SV=t&EV=t&MP3=t&AE=t&VE=t&ACC=t&PR=t&SP=f&SB=f&DEB=f&V=LNX 29,0,0,140&M=Adobe Linux&R=1920x1080&COL=color&AR=1.0&OS=Linux&ARCH=x86&L=en&IME=t&PR32=t&PR64=t&LS=en-US&PT=Desktop&AVD=f&LFD=f&WD=f&TLS=t&ML=5.1&DP=72")
		packet.write32(0).write32(0x6257).writeString('')

		await self.main.send(packet)
		await self.locale.load()

	async def login(self, username, password, encrypted=True, room='1'):
		"""|coro|
		Log in the game.

		:param username: :class:`str` the client username.
		:param password: :class:`str` the client password.
		:param encrypted: Optional[:class:`bool`] whether the password is already encrypted or not.
		:param room: Optional[:class:`str`] the room where the client will be logged in.
		"""
		if not encrypted:
			from .utils import shakikoo
			password = shakikoo(password)

		packet = Packet.new(26, 8).writeString(username).writeString(password)
		packet.writeString("app:/TransformiceAIR.swf/[[DYNAMIC]]/2/[[DYNAMIC]]/4")
		packet.writeString(room).write32(self.authkey^self.keys.auth).write8(0).writeString('')
		packet.cipher(self.keys.identification).write8(0)

		await self.main.send(packet)

	def run(self, api_tfmid, api_token, username, password, **kwargs):
		"""A blocking call that do the event loop initialization for you.

		Equivalent to ::
			@bot.event
			async def on_login_ready(*a):
				await bot.login(username, password)

			loop = asyncio.get_event_loop()
			loop.create_task(bot.start(api_id, api_token))
			loop.run_forever()
		"""
		asyncio.ensure_future(self.start(api_tfmid, api_token, keys=kwargs.pop('keys', None)), loop=self.loop)
		self.loop.run_until_complete(self.wait_for('on_login_ready'))
		asyncio.ensure_future(self.login(username, password, **kwargs), loop=self.loop)

		try:
			self.loop.run_forever()
		except Exception as e:
			# add self.close
			# asyncio.ensure_future(self.close())
			raise e

	def close(self):
		self.main.close()
		if self.bulle is not None:
			self.bulle.close()
		self.loop.stop()

	async def sendCP(self, code, data=b''):
		"""|coro|
		Send a packet to the community platform.

		:param code: :class:`int` the community platform code.
		:param data: :class:`Packet` or :class:`bytes` the data.
		"""
		self.cp_fingerprint = fp = (self.cp_fingerprint + 1) % 0XFFFFFFFF

		packet = Packet.new(60, 3).write16(code)
		packet.write32(self.cp_fingerprint).writeBytes(data)
		await self.main.send(packet, cipher=True)

		return fp

	async def sendRoomMessage(self, message):
		"""|coro|
		Send a message to the room.

		:param message: :class:`str` the content of the message.
		"""
		packet = Packet.new(6, 6).writeString(message)

		await self.bulle.send(packet, cipher=True)

	async def sendTribeMessage(self, message):
		"""|coro|
		Send a message to the tribe.

		:param message: :class:`str` the content of the message.
		"""
		await self.sendCP(50, Packet().writeString(message))

	async def sendChannelMessage(self, channel, message):
		"""|coro|
		Send a message to a public channel.

		:param channel: :class:`str` the channel's name.
		:param message: :class:`str` the content of the message.
		"""
		if isinstance(channel, Channel):
			channel = channel.name

		return await self.sendCP(48, Packet().writeString(channel).writeString(message))

	async def whisper(self, username, message, overflow=False):
		"""|coro|
		Whisper to a player.

		:param username: :class:`str` the player to whisper.
		:param message: :class:`str` the content of the whisper.
		:param overflow: :class:`bool` will send the complete message if True, splitted in several messages.
		"""
		if isinstance(username, Player):
			username = username.username

		async def send(msg):
			await self.sendCP(52, Packet().writeString(username).writeString(msg))

		if isinstance(message, str):
			message = message.encode()
		message = message.replace(b'<', b'&lt;').replace(b'>', b'&gt;')

		await send(message[:255])
		for i in range(255, len(message), 255):
			await asyncio.sleep(1)
			await self.whisper(username, message[i:i+255])

	async def getTribe(self, disconnected=True):
		"""|coro|
		Gets the client's :class:`Tribe` and return it

		:param disconnected: :class:`bool` if True retrieves also the disconnected members.
		:return: :class:`Tribe` or ``None``.
		"""
		sid = self.cp_fingerprint + 1
		await self.sendCP(108, Packet().writeBool(disconnected))
		tc, packet = await self.wait_for('on_raw_cp', lambda tc, p: (tc==109 and p.read32()==sid) or tc==130)
		if tc==109:
			result = packet.readByte()
			if result==1:
				tc, packet = await self.wait_for('on_raw_cp', lambda tc, p: tc==130)
			elif result==17:
				return None
			else:
				raise CommunityPlatformError(118, result)
		return Tribe(packet)

	async def playEmote(self, id, flag='be'):
		"""|coro|
		Play an emote.

		:param id: :class:`int` the emote's id.
		:param flag: Optional[:class:`str`] the flag for the emote id 10. Defaults to 'be'.
		"""
		packet = Packet.new(8, 1).write8(id).write32(0)
		if id==10:
			packet.writeString(flag)

		await self.bulle.send(packet)

	async def sendSmiley(self, id):
		"""|coro|
		Makes the client showing a smiley above it's head.

		:param id: :class:`int` the smiley's id. (from 0 to 9)
		"""
		if id<0 or id>9:
			raise AiotfmException('Invalid smiley id')

		packet = Packet.new(8, 5).write8(id).write32(0)

		await self.bulle.send(packet)

	async def loadLua(self, lua_code):
		"""|coro|
		Load a lua code in the room.

		:param lua_code: :class:`str` or :class:`bytes` the lua code to send.
		"""
		if isinstance(lua_code, str):
			lua_code = lua_code.encode()

		packet = Packet.new(29, 1).write24(len(lua_code)).writeBytes(lua_code)

		await self.bulle.send(packet)

	async def sendCommand(self, command):
		"""|coro|
		Send a command to the game.

		:param command: :class:`str` the command to send.
		"""
		packet = Packet.new(6, 26).writeString(command[:255])

		await self.main.send(packet, cipher=True)

	async def enterTribe(self):
		"""|coro|
		Enter the tribe house
		"""
		await self.main.send(Packet.new(16, 1))

	async def enterTribeHouse(self):
		"""|coro|
		Alias for :meth:`enterTribe`
		"""
		await self.enterTribe()

	async def joinRoom(self, room_name, community=None, auto=False):
		"""|coro|
		Join a room.
		The event 'on_joined_room' is dispatched when the client has successfully joined the room.

		:param room_name: :class:`str` the room's name.
		:param community: Optional[:class:`int`] the room's community.
		:param auto: Optional[:class:`bool`] joins a random room (I think).
		"""
		packet = Packet.new(5, 38).write8(community or self.community)
		packet.writeString(room_name).writeBool(auto)
		await self.main.send(packet)

	async def joinChannel(self, name, permanent=True):
		"""|coro|
		Join a #channel.
		The event 'on_channel_joined' is dispatched when the client has successfully joined a channel.

		:param name: :class:`str` the channel's name
		:param permanent: Optional[:class:`bool`] if True (default) the server will automatically reconnect the user to this channel when logged in.
		"""
		await self.sendCP(54, Packet().writeString(name).writeBool(permanent))

	async def leaveChannel(self, channel):
		"""|coro|
		Leaves a #channel.

		:param channel: :class:`aiotfm.Channel` channel to leave.
		"""
		if isinstance(channel, Channel):
			name = channel.name
		else:
			name = channel

		await self.sendCP(56, Packet().writeString(name))

	async def enterInvTribeHouse(self, author):
		"""|coro|
		Join the tribe house of another player after receiving an /inv.

		:param author: :class:`str` the author's username who sent the invitation.
		"""
		await self.main.send(Packet.new(16, 2).writeString(author))

	async def recruit(self, player):
		"""|coro|
		Send a recruit request to a player.

		:param player: :class:`str` the player's username you want to recruit.
		"""
		await self.sendCP(78, Packet().writeString(player))

	async def requestShopList(self):
		"""|coro|
		Send a request to the server to get the shop list."""
		await self.main.send(Packet.new(8, 20))

	async def startTrade(self, player):
		"""|coro|
		Starts a trade with the given player.

		:param player: :class:`aiotfm.player.Player` the player to trade with.
		:return: :class:`aiotfm.inventory.Trade` the resulting trade"""
		trade = Trade(self, player)
		self.trades.append(trade)
		trade.alive = True
		trade.on_invite = True
		await trade.accept()
		return trade

	async def requestInventory(self):
		"""|coro|
		Send a request to the server to get the bot's inventory."""
		await self.main.send(Packet.new(31, 1))

	async def on_login_result(self, code, *args):
		self.loop.call_later(5, self.close)
		if code==1:
			raise AlreadyConnected()
		elif code==2:
			raise IncorrectPassword()
		raise LoginError(code)
