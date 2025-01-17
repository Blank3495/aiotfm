from aiotfm.errors import AiotfmException

class Room:
	"""Represents the room that the bot currently is in.

	Attributes
	----------
	name: `str`
		The room's name. (i.e: en-1, *bad girls and so on)
	private: `bool`
		Whether the room is public or private.
	players: `list[aiotfm.player.Player]`
		The list containing all the players of the room.
	"""
	def __init__(self, name, private=True):
		self.name = name
		self.private = private
		self.players = []

	def __repr__(self):
		return "<Room name={} private={}>".format(self.name, self.private)

	@property
	def community(self):
		"""Returns the room's community."""
		if self.name.startswith('*'):
			return 'xx'
		return self.name.split('-', 1)[0]

	@property
	def is_tribe(self):
		"""Returns true if it's a tribe house."""
		return self.name.startswith('*\x03')

	@property
	def display_name(self):
		"""Return the display name of the room.
		It removes the \x03 char from the tribe house and the community from the public rooms."""
		if self.is_tribe:
			return self.name.replace('\x03', '')
		if self.name.startswith('*'):
			return self.name
		return self.name.split('-', 1)[1]

	def get_players(self, predicate, max=None):
		"""Filters players from the room.

		:param predicate: A function that returns a boolean-like result to filter through the players.
		:param max: Optional[:class:`int`] The maximum amount of players to return.
		:return: `Iterable` The filtered players."""
		return [p for p in self.players if predicate(p)][:max]

	def get_player(self, **kwargs):
		"""Gets one player in the room with an identifier.

		:param kwargs: Which identifier to use. Can be either name, username, id or pid.
		:return: :class:`aiotfm.player.Player` The player or None"""
		if len(kwargs)==0:
			raise AiotfmException('You did not provide any identifier.')
		if len(kwargs)>1:
			raise AiotfmException('You cannot filter one player with more than one identifier.')

		identifier, value = next(iter(kwargs.items()))

		if identifier=='name' or identifier=='username':
			def filter(p):
				return p==value
		elif identifier=='id':
			def filter(p):
				return p.id==int(value)
		elif identifier=='pid':
			def filter(p):
				return p.pid==int(value)
		else:
			raise AiotfmException('Invalid filter.')

		result = self.get_players(filter)
		if len(result):
			return result[0]