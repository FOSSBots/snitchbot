#! /usr/bin/env python2.6
# Public domain; bjweeks, MZMcBride; 2011

import collections
import re
import sqlite3
import sre_constants
import os
import time

from twisted.internet import protocol, reactor, task
from twisted.python import log
from twisted.words.protocols import irc
from threading import Thread
import settings

'''
CREATE TABLE rules (
    wiki text,
    type text,
    pattern text,
    channel text,
    ignore integer,
    UNIQUE(wiki, type, pattern, channel, ignore)
);
CREATE TABLE channels(
    name text,
    UNIQUE(name)
);
'''

Rule = collections.namedtuple('Rule', 'wiki, type, pattern, channel, ignore')

COLOR_RE = re.compile(r'(?:\x02|\x03(?:\d{1,2}(?:,\d{1,2})?)?)')


def strip_formatting(message):
    """Strips colors and formatting from IRC messages"""
    return COLOR_RE.sub('', message)

ACTION_RE = re.compile(r'(?P<wiki>.+) \[\[(.+)\]\] (?P<log>.+)  \* (?P<user>.+) \*  (?P<summary>.+)')

DIFF_RE = re.compile(r'''
    (?P<wiki>.*)
    \[\[(?P<page>.*)\]\]\        # page title
    (?P<patrolled>!|)            # patrolled
    (?P<new>N|)                  # new page
    (?P<minor>M|)                # minor edit
    (?P<bot>B|)\                 # bot edit
    (?P<url>.*)\                 # diff url
    \*\ (?P<user>.*?)\ \*\       # user
    \((?P<diff>(\+|-)\d*)\)\     # diff size
    ?(?P<summary>.*)             # edit summary
''', re.VERBOSE)


class EternalClient(irc.IRCClient):

    def __init__(self):
        self.pingger = task.LoopingCall(self.pingServer)

    def connectionMade(self):
        irc.IRCClient.connectionMade(self)
        self.pingger.start(60, now=False)

    def connectionLost(self, reason):
        irc.IRCClient.connectionLost(self, reason)
        time.sleep(2)
        self.pingger.stop()

    def pingServer(self):
        self.sendLine('PING %s' % self.transport.connector.host)
        log.msg('%s sent ping to %s'
            % (self.nickname, self.transport.connector.host))

    def irc_PONG(self, prefix, params):
        log.msg('%s received ping from %s'
            % (self.nickname, params[1]))

class Snatch(EternalClient):
    realname = 'Recent Changes'
    nickname = 'StreamBot-read'
    password = ':ZppixBot %s' % (settings.nickserv_password)

    def connectionMade(self):
        EternalClient.connectionMade(self)
        log.msg('Snatch connected')
        self.factory.resetDelay()
        self.factory.snatches.append(self)
        self.channels = set()
        self.cursor = self.factory.connection.cursor()
        self.join('#miraheze-feed')

    def connectionLost(self, reason):
        EternalClient.connectionLost(self, reason)
        log.msg('Snatch disconnected: %s' % reason.value)
        self.factory.snatches.remove(self)
        self.cursor.close()

    def signedOn(self):
        self.syncChannels()

    def joined(self, channel):
        log.msg('Snatch joined %s' % channel)
        self.channels.add(channel)

    def left(self, channel):
        log.msg('Snatch left %s' % channel)
        self.channels.discard(channel)

    def privmsg(self, user, channel, message):
        content = message.split(' ')
        wiki = content[0]
        cleaned_message = message.encode().decode('utf-8', 'ignore')
        cleaned_message = strip_formatting(message)
        edit_match = DIFF_RE.match(cleaned_message)
        action_match = ACTION_RE.match(cleaned_message)
        match = edit_match or action_match
        if not match:
            try:
                log.msg('%s was not matched' % repr(cleaned_message))
                log.msg('Wiki: %s Page: %s User: %s' % wiki)
            except:
                return
            return
        diff = match.groupdict()
        self.cursor.execute(
            'SELECT * FROM rules WHERE wiki=? ORDER BY ignore DESC', (wiki,))
        rule_list = [Rule(*row) for row in self.cursor.fetchall()]

        ignore = []
        for rule in rule_list:
            if rule.channel in ignore:
                continue
            pattern = re.compile(r'^%s$' % rule.pattern, re.I|re.U)
            if rule.type == 'all':
                pass
            elif rule.type == 'summary':
                if 'page' in diff:
                    if not pattern.search(diff['summary']):
                        continue
                else:
                    if not pattern.search( diff['summary']):
                        continue
            elif rule.type == 'user':
                if not pattern.search(diff['user']):
                    continue
            elif rule.type == 'page':
                if 'page' in diff:
                    if not pattern.search(diff['page']):
                        continue
                else:
                    if not pattern.search(diff['summary']):
                        continue
            elif rule.type == 'log':
                if 'log' in diff:
                    if not rule.pattern.lower() == diff['log']:
                        continue
                else:
                    continue
            if rule.ignore:
                ignore.append(rule.channel)
            else:
                for snitch in self.factory.snitches:
                    sendthread = Thread(target=snitch.tattle,args=(rule, diff, wiki))
                    sendthread.run()
                    ignore.append(rule.channel)

    def syncChannels(self):
        log.msg('Syncing snatch\'s channels')
        self.cursor.execute(
            'SELECT wiki FROM rules')
        channels = set('#miraheze-feed')
        [self.join(channel) for channel in (channels - self.channels)]
        [self.part(channel) for channel in (self.channels - channels)]

    def quit(self):
        irc.IRCClient.quit(self)
        self.factory.stopTrying()
        self.transport.loseConnection()


class Snitch(EternalClient):
    realname = 'Recent Changes'
    nickname = '%s' % (settings.nickname)
    password = ':ZppixBot %s' % (settings.nickserv_password)
    lineRate = 1

    def connectionMade(self):
        EternalClient.connectionMade(self)
        log.msg('Snitch connected')
        self.factory.resetDelay()
        self.channels = set()
        self.factory.snitches.append(self)
        self.cursor = self.factory.connection.cursor()

    def connectionLost(self, reason):
        EternalClient.connectionLost(self, reason)
        log.msg('Snitch disconnected: %s' % reason.value)
        self.factory.snitches.remove(self)
        self.cursor.close()

    def signedOn(self):
        self.cursor.execute(
            'SELECT name FROM channels')
        for row in self.cursor.fetchall():
            self.channels.add(row[0])
            self.join(row[0])

    def joined(self, channel):
        log.msg('Snitch joined: %s' % channel)
        self.channels.add(channel)

    def left(self, channel):
        log.msg('Snitch left %s' % channel)
        self.channels.discard(channel)

    def updateRules(self, channel, params, ignore=False, remove=False):
        if len(params) < 2:
            self.msg(channel,
                '!(un)watch wiki (page|user|summary|log|all) [pattern]')
            return
        wiki = params[0]
        rule_type = params[1]

        if rule_type not in ('summary', 'user', 'page', 'log', 'all'):
            self.msg(channel, 'Type must be one of: all, user, summary, page, log.')
            return

        if rule_type == 'all':
            pattern = ''
        elif rule_type != 'all' and len(params) < 3:
            self.msg(channel, 'That requires a pattern.')
            return
        else:
            pattern = ' '.join(params[2:])
            try:
                re.compile(pattern)
            except sre_constants.error:
                self.msg(channel, 'Invalid pattern.')
                return
        self.cursor.execute(
            'SELECT * FROM rules WHERE \
            wiki=? AND type=? AND pattern=? AND channel=? AND ignore=?',
            (wiki, rule_type, pattern, channel, ignore))
        exists = self.cursor.fetchone()

        if remove:
            if exists:
                self.cursor.execute(
                    'DELETE FROM rules WHERE \
                    wiki=? AND type=? AND pattern=? AND channel=? AND ignore=?',
                    (wiki, rule_type, pattern, channel, ignore))
                self.msg(channel, 'Rule deleted.')
            else:
                self.msg(channel, 'No such rule.')
        else:
            if exists:
                self.msg(channel, 'Rule already exists.')
            else:
                self.cursor.execute(
                    'INSERT OR REPLACE INTO rules VALUES (?,?,?,?,?)',
                    (wiki, rule_type, pattern, channel, ignore))
                self.msg(channel, 'Rule added.')

    def privmsg(self, sender, channel, message):
        if not sender:
            return # Twisted sucks
        if channel == self.nickname:
            pass
        elif message.startswith('!'):
            message = message[1:]
        else:
            return

        user = sender.split('!', 1)[0]
        hostmask = sender.split('@', 1)[1]
        action = message.split(' ')[0]
        params = message.split(' ')[1:]

        if action == 'watch':
            self.updateRules(channel, params)
        elif action == 'ignore':
            self.updateRules(channel, params, ignore=True)
        elif action == 'unwatch':
            self.updateRules(channel, params, remove=True)
        elif action == 'unignore':
            self.updateRules(channel, params, ignore=True, remove=True)
        elif action == 'info':
            self.msg(channel, 'I am running StreamBot version Alpha. To see my commands say !help')
        elif action == 'list':
            self.cursor.execute(
                'SELECT * FROM rules WHERE channel=?', (channel,))
            rules = [Rule(*row) for row in self.cursor.fetchall()]
            [self.msg(channel, '%s; %s; %s' % (r.wiki, r.type, r.pattern))
                for r in rules]
        elif action == 'join':
            if re.search(r'(monowatchlist|monowiki)', params[0], re.I):
                self.msg(channel, "FUCK OFF.")
            elif not params:
                if hostmask in settings.authorized_users:
                    self.msg(channel, '!join (channel)')
                else:
                    self.msg(channel, 'Permission denied. Login as an administrator to use this command.')
            else:
                if hostmask in settings.authorized_users:
                    self.msg(channel, 'Now joining channel')
                    self.cursor.execute(
                        'INSERT OR IGNORE INTO channels VALUES (?)', (params[0],))
                    self.join(params[0])
                else:
                    self.msg(channel, 'Permission denied. Login as an administrator to use this command.')
        elif action == 'part':
            if hostmask in settings.authorized_users:
                self.msg(channel, 'Now parting channel')
                self.cursor.execute(
                    'DELETE FROM channels WHERE name=?', (params[0],))
                self.part(params[0])
            else:
                self.msg(channel, 'Permission denied. Login as an administrator to use this command.')
        elif action == 'getout':
            self.msg(channel, 'Okay okay!')
            self.cursor.execute(
                'DELETE FROM channels WHERE name=?', (channel,))
            self.part(channel)
        elif action == 'help':
            self.msg(channel,
                '!(watch|ignore|unwatch|unignore|list|join|part|quit)')
        elif action == 'restart':
            if hostmask in settings.authorized_users:
                self.msg(channel, 'Now restarting...')
                log.msg('Got restart command. Running suicide script')
                os.system('/srv/streambot/internal/suicide')
            else:
                self.msg(channel, 'Permission denied. Login as an administrator to use this command.')
        elif action == 'quit':
            if hostmask in settings.authorized_users:
                log.msg('Quitting')
                self.msg(channel, 'Goodbye!')
                self.quit()
                for snatch in self.factory.snatches:
                    snatch.quit()
            else:
                self.msg(channel, 'Permission denied. Login as an administrator to use this command.')
        else:
            if hostmask in settings.authorized_users:
                self.sendLine(message)

        def quit(self):
            irc.IRCClient.quit(self)
            self.factory.stopTrying()
            self.transport.loseConnection()

    def tattle(self, rule, diff, wiki):
        if rule.channel not in self.channels:
            return
        if 'page' in diff:
            if not diff['summary']:
                diff['summary'] = '[none]'
            self.msg(rule.channel, '\2[[%s]]\2 on %s was edited by \2%s\2 with the following comment: %s Link: %s'
                % (diff['page'], wiki, diff['user'], diff['summary'], diff['url'].replace('http://', 'https://')))
        else:
            url = wiki.strip('wiki')
            if diff['log'] == 'requestwiki' or diff['log'] == 'createwiki' or diff['log'] == 'requestwikiedit':
                url = 'https://' + url + '.miraheze.org/wiki/Special:Log/farmer'
            elif diff['log'] == 'unblock':
                url = 'https://' + url + '.miraheze.org/wiki/Special:Log/block'
            elif diff['log'] == 'overwrite':
                url = 'https://' + url + '.miraheze.org/wiki/Special:Log/upload'
            elif diff['log'] == 'gblock2':
                url = 'https://' + url + '.miraheze.org/wiki/Special:Log/gblblock'
            elif diff['log'] == 'setstatus':
                url = 'https://' + url + '.miraheze.org/wiki/Special:Log/globalauth'
            elif diff['log'] == 'thank':
                url = 'https://' + url + '.miraheze.org/wiki/Special:Log/thanks'
            elif diff['log'] == 'settings':
                url = 'https://' + url + '.miraheze.org/wiki/Special:Log/managewiki'
            elif diff['log'] == 'create':
                url = 'https://' + url + '.miraheze.org/wiki/Special:Log/newusers'
            elif diff['log'] == 'hit':
                url = 'https://' + url + '.miraheze.org/wiki/Special:AbuseLog?wpSearchWiki=' + wiki
            else:
                url = 'https://' + url + '.miraheze.org/wiki/Special:Log/' + diff['log']
            self.msg(rule.channel, 'On %s \2%s\2 %s; %s'
                % (wiki, diff['user'], diff['summary'], url))


class SnatchAndSnitch(protocol.ReconnectingClientFactory):

    factories = 0
    snatches = []
    snitches = []
    connection = None

    @classmethod
    def startFactory(cls):
        if not cls.factories:
            cls.connection = sqlite3.connect(settings.database)
            cls.connection.text_factory = str
        cls.factories += 1

    @classmethod
    def stopFactory(cls):
        cls.factories -= 1
        if not cls.factories:
            cls.connection.commit()
            cls.connection.close()
            time.sleep(2)
            reactor.stop()


def main():
    log.startLogging(open('snitch.log', 'w'))
    snatch = SnatchAndSnitch()
    snatch.protocol = Snatch
    snitch = SnatchAndSnitch()
    snitch.protocol = Snitch
    reactor.connectTCP(settings.snatch_network, 6667, snatch)
    reactor.connectTCP(settings.snitch_network, 6667, snitch)
    reactor.run()

if __name__ == '__main__':
    main()
