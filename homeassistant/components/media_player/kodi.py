"""
Support for interfacing with the XBMC/Kodi JSON-RPC API.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/media_player.kodi/
"""
import asyncio
import logging
import urllib

import aiohttp
import voluptuous as vol

import re
import os
from whoosh import fields, index, qparser

from homeassistant.components.media_player import (
    SUPPORT_NEXT_TRACK, SUPPORT_PAUSE, SUPPORT_PREVIOUS_TRACK, SUPPORT_SEEK,
    SUPPORT_PLAY_MEDIA, SUPPORT_VOLUME_MUTE, SUPPORT_VOLUME_SET, SUPPORT_STOP,
    SUPPORT_TURN_OFF, SUPPORT_PLAY, SUPPORT_VOLUME_STEP, MediaPlayerDevice,
    PLATFORM_SCHEMA)
from homeassistant.const import (
    STATE_IDLE, STATE_OFF, STATE_PAUSED, STATE_PLAYING, CONF_HOST, CONF_NAME,
    CONF_PORT, CONF_SSL, CONF_USERNAME, CONF_PASSWORD)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.core import HomeAssistant

REQUIREMENTS = ['jsonrpc-async==0.2']

_LOGGER = logging.getLogger(__name__)

CONF_TURN_OFF_ACTION = 'turn_off_action'

DEFAULT_NAME = 'Kodi'
DEFAULT_PORT = 8080
DEFAULT_TIMEOUT = 5
DEFAULT_SSL = False

TURN_OFF_ACTION = [None, 'quit', 'hibernate', 'suspend', 'reboot', 'shutdown']

SUPPORT_KODI = SUPPORT_PAUSE | SUPPORT_VOLUME_SET | SUPPORT_VOLUME_MUTE | \
    SUPPORT_PREVIOUS_TRACK | SUPPORT_NEXT_TRACK | SUPPORT_SEEK | \
    SUPPORT_PLAY_MEDIA | SUPPORT_STOP | SUPPORT_PLAY | SUPPORT_VOLUME_STEP

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_HOST): cv.string,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
    vol.Optional(CONF_SSL, default=DEFAULT_SSL): cv.boolean,
    vol.Optional(CONF_TURN_OFF_ACTION, default=None): vol.In(TURN_OFF_ACTION),
    vol.Inclusive(CONF_USERNAME, 'auth'): cv.string,
    vol.Inclusive(CONF_PASSWORD, 'auth'): cv.string,
})


@asyncio.coroutine
def async_setup_platform(hass, config, async_add_entities,
                         discovery_info=None):
    """Setup the Kodi platform."""
    host = config.get(CONF_HOST)
    port = config.get(CONF_PORT)
    use_encryption = config.get(CONF_SSL)

    if host.startswith('http://') or host.startswith('https://'):
        host = host.lstrip('http://').lstrip('https://')
        _LOGGER.warning(
            "Kodi host name should no longer conatin http:// See updated "
            "definitions here: "
            "https://home-assistant.io/components/media_player.kodi/")

    entity = KodiDevice(
        hass,
        name=config.get(CONF_NAME),
        host=host, port=port, encryption=use_encryption,
        username=config.get(CONF_USERNAME),
        password=config.get(CONF_PASSWORD),
        turn_off_action=config.get(CONF_TURN_OFF_ACTION))

    yield from async_add_entities([entity], update_before_add=True)


class KodiDevice(MediaPlayerDevice):
    """Representation of a XBMC/Kodi device."""

    def __init__(self, hass, name, host, port, encryption=False, username=None,
                 password=None, turn_off_action=None):
        """Initialize the Kodi device."""
        import jsonrpc_async
        self.hass = hass
        self._name = name

        kwargs = {
            'timeout': DEFAULT_TIMEOUT,
            'session': async_get_clientsession(hass),
        }

        if username is not None:
            kwargs['auth'] = aiohttp.BasicAuth(username, password)
            image_auth_string = "{}:{}@".format(username, password)
        else:
            image_auth_string = ""

        protocol = 'https' if encryption else 'http'

        self._http_url = '{}://{}:{}/jsonrpc'.format(protocol, host, port)
        self._image_url = '{}://{}{}:{}/image'.format(
            protocol, image_auth_string, host, port)

        self._server = jsonrpc_async.Server(self._http_url, **kwargs)

        self._turn_off_action = turn_off_action
        self._players = list()
        self._properties = None
        self._item = None
        self._app_properties = None
        
        dir_index = '/home/oslo/.homeassistant/kodi_index/'
        if not os.path.isdir(dir_index):
            os.makedirs(dir_index)
        if index.exists_in(dir_index):
            self._index = index.open_dir(dir_index)
        else:
            schema = fields.Schema(name=fields.TEXT, id=fields.NUMERIC(unique=True))
            self._index = index.create_in(dir_index, schema)

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @asyncio.coroutine
    def _get_players(self):
        """Return the active player objects or None."""
        import jsonrpc_async
        try:
            return (yield from self._server.Player.GetActivePlayers())
        except jsonrpc_async.jsonrpc.TransportError:
            if self._players is not None:
                _LOGGER.info('Unable to fetch kodi data')
                _LOGGER.debug('Unable to fetch kodi data', exc_info=True)
            return None

    @property
    def state(self):
        """Return the state of the device."""
        if self._players is None:
            return STATE_OFF

        if len(self._players) == 0:
            return STATE_IDLE

        if self._properties['speed'] == 0 and not self._properties['live']:
            return STATE_PAUSED
        else:
            return STATE_PLAYING

    @asyncio.coroutine
    def async_update(self):
        """Retrieve latest state."""
        self._players = yield from self._get_players()

        if self._players is not None and len(self._players) > 0:
            player_id = self._players[0]['playerid']

            assert isinstance(player_id, int)

            self._properties = yield from self._server.Player.GetProperties(
                player_id,
                ['time', 'totaltime', 'speed', 'live']
            )

            self._item = (yield from self._server.Player.GetItem(
                player_id,
                ['title', 'file', 'uniqueid', 'thumbnail', 'artist']
            ))['item']

            self._app_properties = \
                yield from self._server.Application.GetProperties(
                    ['volume', 'muted']
                )
        else:
            self._properties = None
            self._item = None
            self._app_properties = None

    @property
    def volume_level(self):
        """Volume level of the media player (0..1)."""
        if self._app_properties is not None:
            return self._app_properties['volume'] / 100.0

    @property
    def is_volume_muted(self):
        """Boolean if volume is currently muted."""
        if self._app_properties is not None:
            return self._app_properties['muted']

    @property
    def media_content_id(self):
        """Content ID of current playing media."""
        if self._item is not None:
            return self._item.get('uniqueid', None)

    @property
    def media_content_type(self):
        """Content type of current playing media."""
        if self._players is not None and len(self._players) > 0:
            return self._players[0]['type']

    @property
    def media_duration(self):
        """Duration of current playing media in seconds."""
        if self._properties is not None and not self._properties['live']:
            total_time = self._properties['totaltime']

            return (
                total_time['hours'] * 3600 +
                total_time['minutes'] * 60 +
                total_time['seconds'])

    @property
    def media_image_url(self):
        """Image url of current playing media."""
        if self._item is None:
            return None

        url_components = urllib.parse.urlparse(self._item['thumbnail'])
        if url_components.scheme == 'image':
            return '{}/{}'.format(
                self._image_url,
                urllib.parse.quote_plus(self._item['thumbnail']))

    @property
    def media_title(self):
        """Title of current playing media."""
        # find a string we can use as a title
        if self._item is not None:
            return self._item.get(
                'title',
                self._item.get('label', self._item.get('file', 'unknown')))

    @property
    def supported_features(self):
        """Flag media player features that are supported."""
        supported_features = SUPPORT_KODI

        if self._turn_off_action in TURN_OFF_ACTION:
            supported_features |= SUPPORT_TURN_OFF

        return supported_features

    @asyncio.coroutine
    def async_turn_off(self):
        """Execute turn_off_action to turn off media player."""
        if self._turn_off_action == 'quit':
            yield from self._server.Application.Quit()
        elif self._turn_off_action == 'hibernate':
            yield from self._server.System.Hibernate()
        elif self._turn_off_action == 'suspend':
            yield from self._server.System.Suspend()
        elif self._turn_off_action == 'reboot':
            yield from self._server.System.Reboot()
        elif self._turn_off_action == 'shutdown':
            yield from self._server.System.Shutdown()
        else:
            _LOGGER.warning('turn_off requested but turn_off_action is none')

    @asyncio.coroutine
    def async_volume_up(self):
        """Volume up the media player."""
        assert (
            yield from self._server.Input.ExecuteAction('volumeup')) == 'OK'

    @asyncio.coroutine
    def async_volume_down(self):
        """Volume down the media player."""
        assert (
            yield from self._server.Input.ExecuteAction('volumedown')) == 'OK'

    def async_set_volume_level(self, volume):
        """Set volume level, range 0..1.

        This method must be run in the event loop and returns a coroutine.
        """
        return self._server.Application.SetVolume(int(volume * 100))

    def async_mute_volume(self, mute):
        """Mute (true) or unmute (false) media player.

        This method must be run in the event loop and returns a coroutine.
        """
        return self._server.Application.SetMute(mute)

    @asyncio.coroutine
    def async_set_play_state(self, state):
        """Helper method for play/pause/toggle."""
        players = yield from self._get_players()

        if len(players) != 0:
            yield from self._server.Player.PlayPause(
                players[0]['playerid'], state)

    def async_media_play_pause(self):
        """Pause media on media player.

        This method must be run in the event loop and returns a coroutine.
        """
        return self.async_set_play_state('toggle')

    def async_media_play(self):
        """Play media.

        This method must be run in the event loop and returns a coroutine.
        """
        return self.async_set_play_state(True)

    def async_media_pause(self):
        """Pause the media player.

        This method must be run in the event loop and returns a coroutine.
        """
        return self.async_set_play_state(False)

    @asyncio.coroutine
    def async_media_stop(self):
        """Stop the media player."""
        players = yield from self._get_players()

        if len(players) != 0:
            yield from self._server.Player.Stop(players[0]['playerid'])

    @asyncio.coroutine
    def _goto(self, direction):
        """Helper method used for previous/next track."""
        players = yield from self._get_players()

        if len(players) != 0:
            if direction == 'previous':
                # first seek to position 0. Kodi goes to the beginning of the
                # current track if the current track is not at the beginning.
                yield from self._server.Player.Seek(players[0]['playerid'], 0)

            yield from self._server.Player.GoTo(
                players[0]['playerid'], direction)

    def async_media_next_track(self):
        """Send next track command.

        This method must be run in the event loop and returns a coroutine.
        """
        return self._goto('next')

    def async_media_previous_track(self):
        """Send next track command.

        This method must be run in the event loop and returns a coroutine.
        """
        return self._goto('previous')

    @asyncio.coroutine
    def async_media_seek(self, position):
        """Send seek command."""
        players = yield from self._get_players()

        time = {}

        time['milliseconds'] = int((position % 1) * 1000)
        position = int(position)

        time['seconds'] = int(position % 60)
        position /= 60

        time['minutes'] = int(position % 60)
        position /= 60

        time['hours'] = int(position)

        if len(players) != 0:
            yield from self._server.Player.Seek(players[0]['playerid'], time)

    def async_play_media(self, media_type, media_id, **kwargs):
        """Send the play_media command to the media player.

        This method must be run in the event loop and returns a coroutine.
        """
        if media_type == "CHANNEL":
            return self._server.Player.Open(
                {"item": {"channelid": int(media_id)}})
        elif media_type == "PLAYLIST":
            return self._server.Player.Open(
                {"item": {"playlistid": int(media_id)}})
        else:
            return self._server.Player.Open(
                {"item": {"file": str(media_id)}})
            
    @asyncio.coroutine
    def async_add_song_to_playlist(self, song_id):
        return self._server.Playlist.Add({"playlistid": 0, "item": {"songid": int(song_id)}})
    
    @asyncio.coroutine
    def async_clear_playlist(self):
        return self._server.Playlist.Clear({"playlistid": 0})
    
    @asyncio.coroutine
    def async_play_song(self, song_name):
        song_id = yield from self.async_find_song(song_name)
        
        yield from self.async_media_stop()
        yield from self.async_clear_playlist()
        yield from self.async_add_song_to_playlist(song_id)
        
        yield from self.async_play_media("PLAYLIST", 0)
    
    @asyncio.coroutine
    def async_get_artists(self):
        return (yield from self._server.AudioLibrary.GetArtists())
    
    @asyncio.coroutine
    def async_get_albums(self, artist_id=None):
        if artist_id is None:
            return (yield from self._server.AudioLibrary.GetAlbums())
        else:
            return (yield from self._server.AudioLibrary.GetAlbums(
                {"filter": {"artistid": int(artist_id)}}))
                
    def find_artist(self, artist_name):
        artists = asyncio.get_event_loop().run_until_complete(self.async_get_artists())
        out = self._find(artist_name, [a['artist'] for a in artists['artists']])
        return artists['artists'][out[0][0]]['artistid']

    @asyncio.coroutine
    def async_get_songs(self, artist_id=None):
        if artist_id is None:
            return (yield from self._server.AudioLibrary.GetSongs())
        else:
            return (yield from self._server.AudioLibrary.GetSongs(
                {"filter": {"artistid": int(artist_id)}}))
    
    @asyncio.coroutine     
    def async_find_song(self, song_name):
        songs = yield from self.async_get_songs()
        out = self._find(song_name, [a['label'] for a in songs['songs']])
        return songs['songs'][out[0][0]]['songid']
    
    @asyncio.coroutine
    def async_update_index(self):
        songs = yield from self.async_get_songs()
        wr = self._index.writer()
        for s in songs['songs']:
            wr.update_document(name=s['label'], id=int(s['songid']))
        wr.commit()
                
    def _find(self, key_word, words):
        key_word = key_word.split(' ')
        patt = [re.compile(k, re.IGNORECASE) for k in key_word]
         
        out = [[i, 0] for i in range(len(words))]
        for i in range(len(words)):
            mt = [p.search(words[i]) for p in patt]
            l = [m is not None for m in mt].count(True)
            out[i][1] = l
            
        return sorted(out, key=lambda out: out[1], reverse=True)    
    
if __name__ == '__main__':
    kodi = KodiDevice(HomeAssistant(), '', '192.168.0.33', '8080')
    
    #songs = asyncio.get_event_loop().run_until_complete(kodi.async_get_songs())
    #print(songs['songs'])
    asyncio.get_event_loop().run_until_complete(kodi.async_update_index())
    
    qp = qparser.QueryParser("name", schema=kodi._index.schema)
    q = qp.parse(u"donna amico")
    
    with kodi._index.searcher() as s:
        results = s.search(q)
        
        print(results[0:1])
        
    
    
