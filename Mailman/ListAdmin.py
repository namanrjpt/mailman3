# Copyright (C) 1998,1999,2000,2001,2002 by the Free Software Foundation, Inc.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software 
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.

"""Mixin class for MailList which handles administrative requests.

Two types of admin requests are currently supported: adding members to a
closed or semi-closed list, and moderated posts.

Pending subscriptions which are requiring a user's confirmation are handled
elsewhere.
"""

import os
import time
import marshal
import errno
import cPickle
import email
from email.MIMEMessage import MIMEMessage
from cStringIO import StringIO

from Mailman import mm_cfg
from Mailman import Utils
from Mailman import Message
from Mailman import Errors
from Mailman.UserDesc import UserDesc
from Mailman.Queue.sbcache import get_switchboard
from Mailman.Logging.Syslog import syslog
from Mailman.i18n import _

# Request types requiring admin approval
IGN = 0
HELDMSG = 1
SUBSCRIPTION = 2
UNSUBSCRIPTION = 3

# Return status from __handlepost()
DEFER = 0
REMOVE = 1
LOST = 2

DASH = '-'
NL = '\n'



class ListAdmin:
    def InitVars(self):
        # non-configurable data
        self.next_request_id = 1

    def InitTempVars(self):
        self.__db = None

    def __filename(self):
        return os.path.join(self.fullpath(), 'request.db')

    def __opendb(self):
        filename = self.__filename()
        if self.__db is None:
            assert self.Locked()
            try:
                fp = open(filename)
                self.__db = marshal.load(fp)
                fp.close()
            except IOError, e:
                if e.errno <> errno.ENOENT: raise
                self.__db = {}
            except EOFError, e:
                # The unmarshalling failed, which means the file is corrupt.
                # Sigh. Start over.
                syslog('error',
                       'request.db file corrupt for list %s, blowing it away.',
                       self.internal_name())
                self.__db = {}
            # Migrate pre-2.1a3 held subscription records to include the
            # fullname data field.
            type, version = self.__db.get('version', (IGN, None))
            if version is None:
                # No previous revisiont number, must be upgrading to 2.1a3 or
                # beyond from some unknown earlier version.
                for id, (type, data) in self.__db.items():
                    if id == IGN:
                        pass
                    elif id == HELDMSG and len(data) == 5:
                        # tack on a msgdata dictionary
                        self.__db[id] = data + ({},)
                    elif id == SUBSCRIPTION and len(data) == 5:
                        # a fullname field was added
                        stime, addr, password, digest, lang = data
                        self.__db[id] = stime, addr, '', password, digest, lang
                        

    def __closedb(self):
        if self.__db is not None:
            assert self.Locked()
            # Save the version number
            self.__db['version'] = IGN, mm_cfg.REQUESTS_FILE_SCHEMA_VERSION
            # Now save a temp file and do the tmpfile->real file dance.  BAW:
            # should we be as paranoid as for the config.pck file?  Should we
            # use pickle?
            tmpfile = self.__filename() + '.tmp'
            omask = os.umask(002)
            try:
                fp = open(tmpfile, 'w')
                marshal.dump(self.__db, fp)
                fp.close()
                self.__db = None
            finally:
                os.umask(omask)
            # Do the dance
            os.rename(tmpfile, self.__filename())

    def __request_id(self):
	id = self.next_request_id
	self.next_request_id += 1
	return id

    def SaveRequestsDb(self):
        self.__closedb()

    def NumRequestsPending(self):
        self.__opendb()
        # Subtrace one for the version pseudo-entry
        if self.__db.has_key('version'):
            return len(self.__db) - 1
        return len(self.__db)

    def __getmsgids(self, rtype):
        self.__opendb()
        ids = [k for k, (type, data) in self.__db.items() if type == rtype]
        ids.sort()
        return ids

    def GetHeldMessageIds(self):
        return self.__getmsgids(HELDMSG)

    def GetSubscriptionIds(self):
        return self.__getmsgids(SUBSCRIPTION)

    def GetUnsubscriptionIds(self):
        return self.__getmsgids(UNSUBSCRIPTION)

    def GetRecord(self, id):
        self.__opendb()
        type, data = self.__db[id]
        return data

    def GetRecordType(self, id):
        self.__opendb()
        type, data = self.__db[id]
        return type

    def HandleRequest(self, id, value, comment=None, preserve=None,
                      forward=None, addr=None):
        self.__opendb()
        rtype, data = self.__db[id]
        if rtype == HELDMSG:
            status = self.__handlepost(data, value, comment, preserve,
                                       forward, addr)
        elif rtype == UNSUBSCRIPTION:
            status = self.__handleunsubscription(data, value, comment)
        else:
            assert rtype == SUBSCRIPTION
            status = self.__handlesubscription(data, value, comment)
        if status <> DEFER:
            # BAW: Held message ids are linked to Pending cookies, allowing
            # the user to cancel their post before the moderator has approved
            # it.  We should probably remove the cookie associated with this
            # id, but we have no way currently of correlating them. :(
            del self.__db[id]

    def HoldMessage(self, msg, reason, msgdata={}):
        # Make a copy of msgdata so that subsequent changes won't corrupt the
        # request database.  TBD: remove the `filebase' key since this will
        # not be relevant when the message is resurrected.
        newmsgdata = {}
        newmsgdata.update(msgdata)
        msgdata = newmsgdata
        # assure that the database is open for writing
        self.__opendb()
        # get the next unique id
        id = self.__request_id()
        assert not self.__db.has_key(id)
        # get the message sender
        sender = msg.get_sender()
        # calculate the file name for the message text and write it to disk
        filename = 'heldmsg-%s-%d.pck' % (self.internal_name(), id)
        omask = os.umask(002)
        fp = None
        try:
            fp = open(os.path.join(mm_cfg.DATA_DIR, filename), 'w')
            cPickle.dump(msg, fp, 1)
        finally:
            if fp:
                fp.close()
            os.umask(omask)
        # save the information to the request database.  for held message
        # entries, each record in the database will be of the following
        # format:
        #
        # the time the message was received
        # the sender of the message
        # the message's subject
        # a string description of the problem
        # name of the file in $PREFIX/data containing the msg text
        # an additional dictionary of message metadata
        #
        msgsubject = msg.get('subject', _('(no subject)'))
        data = time.time(), sender, msgsubject, reason, filename, msgdata
        self.__db[id] = (HELDMSG, data)
        return id

    def __handlepost(self, record, value, comment, preserve, forward, addr):
        # For backwards compatibility with pre 2.0beta3
        ptime, sender, subject, reason, filename, msgdata = record
        path = os.path.join(mm_cfg.DATA_DIR, filename)
        # Handle message preservation
        if preserve:
            parts = os.path.split(path)[1].split(DASH)
            parts[0] = 'spam'
            spamfile = DASH.join(parts)
            import shutil
            try:
                shutil.copy(path, os.path.join(mm_cfg.SPAM_DIR, spamfile))
            except IOError, e:
                if e.errno <> errno.ENOENT: raise
                return LOST
        # Now handle updates to the database
        rejection = None
        fp = None
        msg = None
        status = REMOVE
        if value == mm_cfg.DEFER:
            # Defer
            status = DEFER
        elif value == mm_cfg.APPROVE:
            # Approved.
            try:
                msg = readMessage(path)
            except IOError, e:
                if e.errno <> errno.ENOENT: raise
                return LOST
            msg = readMessage(path)
            msgdata['approved'] = 1
            # Calculate a new filebase for the approved message, otherwise
            # delivery errors will cause duplicates.
            try:
                del msgdata['filebase']
            except KeyError:
                pass
            # Queue the file for delivery by qrunner.  Trying to deliver the
            # message directly here can lead to a huge delay in web
            # turnaround.  Log the moderation and add a header.
            msg['X-Moderated'] = '<%s> %s' % (self.GetOwnerEmail(),
                                              email.Utils.formatdate())
            syslog('vette', 'held message approved, message-id: %s',
                   msg.get('message-id', 'n/a'))
            # Stick the message back in the incoming queue for further
            # processing.
            inq = get_switchboard(mm_cfg.INQUEUE_DIR)
            inq.enqueue(msg, _metadata=msgdata)
        elif value == mm_cfg.REJECT:
            # Rejected
            rejection = 'Refused'
            self.__refuse(_('Posting of your message titled "%(subject)s"'),
                          sender, comment or _('[No reason given]'),
                          lang=self.getMemberLanguage(sender))
        else:
            assert value == mm_cfg.DISCARD
            # Discarded
            rejection = 'Discarded'
        # Forward the message
        if forward and addr:
            # If we've approved the message, we need to be sure to craft a
            # completely unique second message for the forwarding operation,
            # since we don't want to share any state or information with the
            # normal delivery.
            try:
                copy = readMessage(path)
            except IOError, e:
                if e.errno <> errno.ENOENT: raise
                raise Errors.LostHeldMessage(path)
            fmsg = Message.UserNotification(addr,
                                            self.GetAdminEmail(),
                                            _('Forward of moderated  message'))
            fmsg['Content-Type'] = 'message/rfc822'
            fmsg['MIME-Version'] = '1.0'
            fmsg.set_payload(copy)
            fmsg.send(self)
        # Log the rejection
	if rejection:
            note = '''%(listname)s: %(rejection)s posting:
\tFrom: %(sender)s
\tSubject: %(subject)s''' % {
                'listname' : self.internal_name(),
                'rejection': rejection,
                'sender'   : sender.replace('%', '%%'),
                'subject'  : subject.replace('%', '%%'),
                }
            if comment:
                note += '\n\tReason: ' + comment.replace('%', '%%')
            syslog('vette', note)
        # Always unlink the file containing the message text.  It's not
        # necessary anymore, regardless of the disposition of the message.
        if status <> DEFER:
            try:
                os.unlink(path)
            except OSError, e:
                if e.errno <> errno.ENOENT: raise
                # We lost the message text file.  Clean up our housekeeping
                # and inform of this status.
                return LOST
        return status
            
    def HoldSubscription(self, addr, fullname, password, digest, lang):
        # Assure that the database is open for writing
        self.__opendb()
        # Get the next unique id
        id = self.__request_id()
        assert not self.__db.has_key(id)
        #
        # Save the information to the request database. for held subscription
        # entries, each record in the database will be one of the following
        # format:
        #
        # the time the subscription request was received
        # the subscriber's address
        # the subscriber's selected password (TBD: is this safe???)
        # the digest flag
	# the user's preferred language
        #
        data = time.time(), addr, fullname, password, digest, lang
        self.__db[id] = (SUBSCRIPTION, data)
        #
        # TBD: this really shouldn't go here but I'm not sure where else is
        # appropriate.
        syslog('vette', '%s: held subscription request from %s',
               self.internal_name(), addr)
        # Possibly notify the administrator in default list language
        if self.admin_immed_notify:
            realname = self.real_name
            subject = _(
                'New subscription request to list %(realname)s from %(addr)s')
            text = Utils.maketext(
                'subauth.txt',
                {'username'   : addr,
                 'listname'   : self.internal_name(),
                 'hostname'   : self.host_name,
                 'admindb_url': self.GetScriptURL('admindb', absolute=1),
                 }, mlist=self)
            # This message should appear to come from the <list>-owner so as
            # to avoid any useless bounce processing.
            owneraddr = self.GetOwnerEmail()
            msg = Message.UserNotification(owneraddr, owneraddr, subject, text)
            msg['MIME-Version'] = '1.0'
            msg.add_header('Content-Type', 'text/plain',
                           charset=Utils.GetCharSet(self.preferred_language))
            msg.send(self, **{'tomoderators': 1})

    def __handlesubscription(self, record, value, comment):
        stime, addr, fullname, password, digest, lang = record
        if value == mm_cfg.DEFER:
            return DEFER
        elif value == mm_cfg.DISCARD:
            pass
        elif value == mm_cfg.REJECT:
            self.__refuse(_('Subscription request'), addr, comment, lang=lang)
        else:
            # subscribe
            assert value == mm_cfg.SUBSCRIBE
            try:
                userdesc = UserDesc(addr, fullname, password, digest, lang)
                self.ApprovedAddMember(userdesc)
            except Errors.MMAlreadyAMember:
                # User has already been subscribed, after sending the request
                pass
            # TBD: disgusting hack: ApprovedAddMember() can end up closing
            # the request database.
            self.__opendb()
        return REMOVE

    def HoldUnsubscription(self, addr):
        # Assure the database is open for writing
        self.__opendb()
        # Get the next unique id
        id = self.__request_id()
        assert not self.__db.has_key(id)
        # All we need to do is save the unsubscribing address
        self.__db[id] = (UNSUBSCRIPTION, addr)
        syslog('vette', '%s: held unsubscription request from %s',
               self.internal_name(), addr)
        # Possibly notify the administrator of the hold
        if self.admin_immed_notify:
            realname = self.real_name
            subject = _(
                'New unsubscription request from %(realname)s by %(addr)s')
            text = Utils.maketext(
                'unsubauth.txt',
                {'username'   : addr,
                 'listname'   : self.internal_name(),
                 'hostname'   : self.host_name,
                 'admindb_url': self.GetScriptURL('admindb', absolute=1),
                 }, mlist=self)
            # This message should appear to come from the <list>-owner so as
            # to avoid any useless bounce processing.
            owneraddr = self.GetOwnerEmail()
            msg = Message.UserNotification(owneraddr, owneraddr, subject, text)
            msg['MIME-Version'] = '1.0'
            msg.add_header('Content-Type', 'text/plain',
                           charset=Utils.GetCharSet(self.preferred_language))
            msg.send(self, **{'tomoderators': 1})

    def __handleunsubscription(self, record, value, comment):
        addr = record
        if value == mm_cfg.DEFER:
            return DEFER
        elif value == mm_cfg.DISCARD:
            pass
        elif value == mm_cfg.REJECT:
            self.__refuse(_('Unsubscription request'), addr, comment)
        else:
            assert value == mm_cfg.UNSUBSCRIBE
            try:
                self.ApprovedDeleteMember(addr)
            except Errors.NotAMemberError:
                # User has already been unsubscribed
                pass
        return REMOVE

    def __refuse(self, request, recip, comment, origmsg=None, lang=None):
        adminaddr = self.GetAdminEmail()
        realname = self.real_name
	if lang is None:
            lang = self.preferred_language
        text = Utils.maketext(
            'refuse.txt',
            {'listname' : realname,
             'request'  : request,
             'reason'   : comment,
             'adminaddr': adminaddr,
            }, lang=lang, mlist=self)
        # add in original message, but not wrap/filled
        if origmsg:
            text = NL.join(
                [text,
                 '---------- ' + _('Original Message') + ' ----------',
                 str(origmsg)
                 ])
        subject = _('Request to mailing list %(realname)s rejected')
        msg = Message.UserNotification(recip, adminaddr, subject, text)
        msg.send(self)



def readMessage(path):
    # For backwards compatibility, we must be able to read either a flat text
    # file or a pickle.
    ext = os.path.splitext(path)[1]
    fp = open(path)
    try:
        if ext == '.txt':
            msg = email.message_from_file(fp, Message.Message)
        else:
            assert ext == '.pck'
            msg = cPickle.load(fp)
    finally:
        fp.close()
    return msg
