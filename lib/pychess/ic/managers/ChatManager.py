#from gobject import *

import threading
import re
from math import ceil
import time
import operator

from gi.repository import GLib
from gi.repository import GObject

from pychess.compat import unichr
from pychess.System.Log import log
from pychess.ic.FICSObjects import FICSPlayer
from pychess.ic.__init__ import *

titles = "(?:\([A-Z*]+\))*"
names = "([A-Za-z]+)"+titles
titlesC = re.compile(titles)
namesC = re.compile(names)
ratings = "\(\s*([0-9\ \-\+]{1,4}[P E]?|UNR)\)"

CHANNEL_SHOUT = "shout"
CHANNEL_CSHOUT = "cshout"

class ChatManager (GObject.GObject):

    __gsignals__ = {
        'channelMessage' : (GObject.SignalFlags.RUN_FIRST, None, (str, bool, bool, str, str)),
        'kibitzMessage' : (GObject.SignalFlags.RUN_FIRST, None, (str, int, str)),
        'whisperMessage' : (GObject.SignalFlags.RUN_FIRST, None, (str, int, str)),
        'privateMessage' : (GObject.SignalFlags.RUN_FIRST, None, (str, str, bool, str)),
        'bughouseMessage' : (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        'announcement' : (GObject.SignalFlags.RUN_FIRST, None, (str,)),

        'arrivalNotification': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        'departedNotification': (GObject.SignalFlags.RUN_FIRST, None, (object,)),

        'channelAdd' : (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'channelRemove' : (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'channelJoinError': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        'channelsListed': (GObject.SignalFlags.RUN_FIRST, None, (object,)),

        'channelLog' : (GObject.SignalFlags.RUN_FIRST, None, (str, int, str, str)),
        'toldChannel' : (GObject.SignalFlags.RUN_FIRST, None, (str, int)),

        'receivedChannels' : (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
        'receivedNames' : (GObject.SignalFlags.RUN_FIRST, None, (str, object)),

        'observers_received' : (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    def __init__ (self, connection):
        GObject.GObject.__init__(self)
        self.connection = connection

        self.connection.expect_line (self.onPrivateMessage,
                "%s(\*)?(?:\[\d+\])? (?:tells you|says): (.*)" % names)
        self.connection.expect_line (self.onAnnouncement,
                "\s*\*\*ANNOUNCEMENT\*\* (.*)")
        self.connection.expect_line (self.onChannelMessage,
                "%s(\*)?\((\d+)\): (.*)" % names)
        self.connection.expect_line (self.onShoutMessage,
                "%s(\*)? (c-)?shouts: (.*)" % names)
        self.connection.expect_line (self.onShoutMessage,
                "--> %s(\*)?:? (.*)" % names)
        self.connection.expect_line (self.onKibitzMessage,
                "%s%s\[(\d+)\] kibitzes: (.*)" % (names, ratings))
        self.connection.expect_line (self.onWhisperMessage,
                "%s%s\[(\d+)\] whispers: (.*)" % (names, ratings))

        self.connection.expect_line(self.onArrivalNotification,
                                    "Notification: %s has arrived\." % names)
        self.connection.expect_line(self.onDepartedNotification,
                                    "Notification: %s has departed\." % names)

        self.connection.expect_fromto (self.onChannelList,
                "channels only for their designated topics.",
                "SPECIAL NOTE")

        self.connection.expect_line (lambda m: GLib.idle_add(self.emit, 'channelAdd', m.groups()[0]),
                "\[(\d+)\] added to your channel list.")
        self.connection.expect_line (lambda m: GLib.idle_add(self.emit, 'channelRemove', m.groups()[0]),
                "\[(\d+)\] removed to your channel list.")

        self.connection.expect_line (lambda m: GLib.idle_add(self.emit, 'channelJoinError', *m.groups()),
                "Only (.+?) may join channel (\d+)\.")

        self.connection.expect_line (self.getNoChannelPlayers,
                "Channel (\d+) is empty\.")
        self.connection.expect_fromto (self.getChannelPlayers,
                "Channel (\d+)(?: \"(\w+)\")?: (.+)",
                "(\d+) player(?: is|s are) in channel \d+\.")

        self.connection.expect_fromto (self.gotPlayerChannels,
                "%s is in the following channels:" % names,
                "(?!(?:\d+\s+)+)")

        #self.connection.expect_line (self.toldChannel,
        #        '\(told (\d+) players in channel (\d+) ".+"\)')
        #(told Chronatog)

        #Only chess advisers and admins may join channel 63.
        #Only (.+?) may sey send tells to channel (\d+).
        #Only admins may send tells to channel 0.
        #Only registered users may send tells to channels other than 4, 7 and 53.

        self.currentLogChannel = None
        self.connection.expect_line (self.onChannelLogStart,
                ":Channel (\d+|shout|c-shout) log for the last \d+ minutes:$")
        self.connection.expect_line (self.onChannelLogLine,
                ":\[(\d+):(\d+):(\d+)\] (?:(?:--> )?%s(?: shouts)?)\S* (.+)$" % names)
        self.connection.expect_line (self.onChannelLogBreak,
                ":Use \"tell chLog Next\" to print more.$")

        #TODO handling of this case is nessesary for console:
        #fics% tell 1 hi
        #You are not in channel 1, auto-adding you if possible.

        # Setting 'Lang' is a workaround for
        # http://code.google.com/p/pychess/issues/detail?id=376
        # and can be removed when conversion to FICS block mode is done
        self.connection.lvm.setVariable("Lang", "English")

        # We shouldn't touch any user variables
        ###self.connection.lvm.setVariable("kibitz", 0)
        ###self.connection.lvm.setVariable("ctell", 1)
        ###self.connection.lvm.setVariable("tell", 1)

        self.connection.lvm.setVariable("height", 240)

        self.connection.client.run_command("inchannel %s" % self.connection.username)
        self.connection.client.run_command("help channel_list")
        self.channels = {}

        #Observing 112 [DrStupp vs. hajaK]: pgg (1 user)
        self.connection.expect_line (self.get_allob_List,
                                     'Observing\s+(\d+).*:([\s+\(\w+\)\w+]+)\(' )


    def get_allob_List(self,match):
        """ Description: Processes the returning pattern matched of the FICS allob command
                         extracts out the gameno and a list of observers before emmiting them for collection
                         by the observers view
            match: (re.reg-ex) is the complied matching pattern for processing
        """

        obs_dic = {}
        gameno = match.group(1)
        observers = match.group(2)
        oblist = observers.split()
        for player in oblist:
            if '(U)' not in player :
                try:
                    ficsplayer = self.connection.players[FICSPlayer(player)]
                    obs_dic[player] = ficsplayer.getRatingByGameType(GAME_TYPES['standard'])
                except KeyError:
                    obs_dic[player] =  0
                    print("player %s is not in self.connection.players" % player) 
            else :
                obs_dic[player] =  0
        obs_sorted  = sorted(obs_dic.items(), key=operator.itemgetter(1),reverse=True)
        obs_str = "\n"
        for toople in obs_sorted :
            player,rating = toople
            if rating == 0 :
                obs_str += player + " "        # Don't print ratings for guest accounts
            else:
                obs_str += player + "[" + str(rating) + "] "
        self.emit('observers_received',gameno,obs_str)


    def getChannels(self):
        return self.channels

    def getJoinedChannels(self):
        channels = self.connection.lvm.getList("channel")
        if self.connection.lvm.getVariable("shout"):
            channels.add(CHANNEL_SHOUT)
        if self.connection.lvm.getVariable("cshout"):
            channels.add(CHANNEL_CSHOUT)
        return channels

    channelListItem = re.compile("((?:\d+,?)+)\s*(.*)")
    def onChannelList(self, matchlist):
        self.channels = [(CHANNEL_SHOUT, _("Shout")),
                         (CHANNEL_CSHOUT, _("Chess Shout"))]
        numbers = set(range(256)) #TODO: Use limits->Server->Channels
        for line in matchlist[1:-1]:
            match = self.channelListItem.match(line)
            if not match: continue
            ids, desc = match.groups()
            for id in ids.split(","):
                numbers.remove(int(id))
                self.channels.append((id, desc))
        for i in numbers:
            self.channels.append((str(i), _("Unofficial channel %d" % i)))

        GLib.idle_add(self.emit, 'channelsListed', self.channels)

    def getNoChannelPlayers (self, match):
        channel = match.groups()[0]
        GLib.idle_add(self.emit, 'receivedNames', channel, [])

    def getChannelPlayers(self, matchlist):
        channel, name, people = matchlist[0].groups()
        people += " " + " ".join(matchlist[1:-1])
        people = namesC.findall(titlesC.sub("",people))
        GLib.idle_add(self.emit, 'receivedNames', channel, people)

    def gotPlayerChannels(self, matchlist):
        name = matchlist[0].groups()
        list = []
        for line in matchlist[1:-1]:
            list += line.split()


    def onPrivateMessage (self, match):
        name, isadmin, text = match.groups()
        text = self.entityDecode(text)
        GLib.idle_add(self.emit, "privateMessage", name, "title", isadmin, text)

    def onAnnouncement (self, match):
        text = match.groups()[0]
        text = self.entityDecode(text)
        GLib.idle_add(self.emit, "announcement", text)

    def onChannelMessage (self, match):
        name, isadmin, channel, text = match.groups()
        text = self.entityDecode(text)
        isme = name.lower() == self.connection.username.lower()
        GLib.idle_add(self.emit, "channelMessage", name, isadmin, isme, channel, text)

    def onShoutMessage (self, match):
        if len(match.groups()) == 4:
            name, isadmin, type, text = match.groups()
        elif len(match.groups()) == 3:
            name, isadmin, text = match.groups()
            type = ""
        text = self.entityDecode(text)
        isme = name.lower() == self.connection.username.lower()
        # c-shout should be used ONLY for chess-related messages, such as
        # questions about chess or announcing being open for certain kinds of
        # chess matches. Use "shout" for non-chess messages.

        # t-shout is used to invite to tournaments
        if type == "c-":
            GLib.idle_add(self.emit, "channelMessage", name, isadmin, isme, CHANNEL_CSHOUT, text)
        else:
            GLib.idle_add(self.emit, "channelMessage", name, isadmin, isme, CHANNEL_SHOUT, text)

    def onKibitzMessage (self, match):
        name, rating, gameno, text = match.groups()
        text = self.entityDecode(text)
        GLib.idle_add(self.emit, "kibitzMessage", name, int(gameno), text)

    def onWhisperMessage (self, match):
        name, rating, gameno, text = match.groups()
        text = self.entityDecode(text)
        GLib.idle_add(self.emit, "whisperMessage", name, int(gameno), text)

    def onArrivalNotification (self, match):
        name = match.groups()[0]
        try:
            player = self.connection.players.get(FICSPlayer(name))
        except KeyError:
            return
        if player.name not in self.connection.notify_users:
            self.connection.notify_users.append(player.name)
        GLib.idle_add(self.emit, "arrivalNotification", player)

    def onDepartedNotification (self, match):
        name = match.groups()[0]
        try:
            player = self.connection.players.get(FICSPlayer(name))
        except KeyError:
            return
        GLib.idle_add(self.emit, "departedNotification", player)

    def toldChannel (self, match):
        amount, channel = match.groups()
        GLib.idle_add(self.emit, "toldChannel", channel, int(amount))

    def onChannelLogStart (self, match):
        channel, = match.groups()
        self.currentLogChannel = channel

    def onChannelLogLine (self, match):
        if not self.currentLogChannel:
            log.warning("Received log line before channel was set")
            return
        h, m, s, handle, text = match.groups()
        time = self.convTime(int(h), int(m), int(s))
        text = self.entityDecode(text)
        GLib.idle_add(self.emit, "channelLog", self.currentLogChannel, time, handle, text)

    def onChannelLogBreak (self, match):
        self.connection.client.run_command("xtell chlog Next")


    def convTime (self, h, m, s):
        # Convert to timestamp
        t1, t2, t3, t4, t5, t6, t7, t8, t9 = time.localtime()
        tstamp = time.mktime((t1, t2, t3, h, m, s, 0, 0, 0))
        # Difference to now in hours
        dif = (tstamp-time.time())/60./60.
        # As we know there is maximum 30 minutes in difference, we can guess when the
        # message was sent, without knowing the sending time zone
        return tstamp - ceil(dif)*60*60

    entityExpr = re.compile("&#x([a-f0-9]+);")
    def entityDecode (self, text):
        return self.entityExpr.sub(lambda m: unichr(int(m.groups()[0],16)), text)

    def entityEncode (self, text):
        buf = []
        for c in text:
            if not 32 <= ord(c) <= 127:
                c = "&#" + hex(ord(c))[1:] + ";"
            buf.append(c)
        return "".join(buf)


    def getChannelLog (self, channel, minutes=30):
        """ Channel can be channel_id, shout or c-shout """
        assert 1 <= minutes <= 120
        # Using the chlog bot
        self.connection.client.run_command("xtell chlog show %s -t %d" % (channel,minutes))

    def getPlayersChannels (self, player):
        self.connection.client.run_command("inchannel %s" % player)

    def getPeopleInChannel (self, channel):
        if channel in (CHANNEL_SHOUT, CHANNEL_CSHOUT):
            people = self.connection.players.get_online_playernames()
            GLib.idle_add(self.emit, 'receivedNames', channel, people)
        self.connection.client.run_command("inchannel %s" % channel)

    def joinChannel (self, channel):
        if channel in (CHANNEL_SHOUT, CHANNEL_CSHOUT):
            self.connection.lvm.setVariable(channel, 1)
        self.connection.client.run_command("+channel %s" % channel)

    def removeChannel (self, channel):
        if channel in (CHANNEL_SHOUT, CHANNEL_CSHOUT):
            self.connection.lvm.setVariable(channel, 0)
        self.connection.client.run_command("-channel %s" % channel)


    def mayTellChannel (self, channel):
        if self.connection.isRegistred() or channel in ("4", "7", "53"):
            return True
        return False

    def tellPlayer (self, player, message):
        message = self.entityEncode(message)
        self.connection.client.run_command("tell %s %s" % (player, message))

    def tellChannel (self, channel, message):
        message = self.entityEncode(message)
        if channel == CHANNEL_SHOUT:
            self.connection.client.run_command("shout %s" % message)
        elif channel == CHANNEL_CSHOUT:
            self.connection.client.run_command("cshout %s" % message)
        else:
            self.connection.client.run_command("tell %s %s" % (channel, message))

    def tellAll (self, message):
        message = self.entityEncode(message)
        self.connection.client.run_command("shout %s" % message)

    def tellGame (self, gameno, message):
        message = self.entityEncode(message)
        self.connection.client.run_command("xkibitz %s %s" % (gameno, message))

    def tellOpponent (self, message):
        message = self.entityEncode(message)
        self.connection.client.run_command("say %s" % message)

    def tellBughousePartner (self, message):
        message = self.stripChars(message)
        self.connection.client.run_command("ptell %s" % message)

    def tellUser (self, player, message):
        IS_TD = False
        if IS_TD:
            MAX_COM_SIZE = 1024 #TODO: Get from limits
            for i in range(0,len(message),MAX_COM_SIZE):
                chunk = message[i:i+MAX_COM_SIZE]
                chunk = chunk.replace("\n", "\\n")
                chunk = self.entityEncode(chunk)
                self.connection.client.run_command("qell %s %s" % (player, chunk))
        else:
            for line in message.strip().split("\n"):
                self.tellPlayer(player, line)

    def whisper(self, message):
        message = self.entityEncode(message)
        self.connection.client.run_command("whisper %s" % message)
