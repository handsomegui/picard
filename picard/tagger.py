# -*- coding: utf-8 -*-
#
# Picard, the next-generation MusicBrainz tagger
# Copyright (C) 2004 Robert Kaye
# Copyright (C) 2006 Lukáš Lalinský
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

from PyQt4 import QtGui, QtCore

import gettext
import locale
import logging
import os.path
import sys

import picard.resources

from picard.album import Album
from picard.cluster import Cluster
from picard.api import IFileOpener
from picard.browser.filelookup import FileLookup
from picard.browser.browser import BrowserIntegration
from picard.component import ComponentManager, Interface, ExtensionPoint, Component
from picard.config import Config
from picard.ui.mainwindow import MainWindow
from picard.worker import WorkerThread
from picard.file import FileManager

from musicbrainz2.webservice import Query, TrackFilter

# Install gettext "noop" function.
import __builtin__
__builtin__.__dict__['N_'] = lambda a: a 

class Tagger(QtGui.QApplication, ComponentManager, Component):
    
    fileOpeners = ExtensionPoint(IFileOpener)
    
    def __init__(self, localeDir):
        QtGui.QApplication.__init__(self, sys.argv)
        ComponentManager.__init__(self)

        self.config = Config()
        
        logging.basicConfig(level=logging.DEBUG,
#                    format='%(message)s',
                    format='%(asctime)s %(levelname)-8s %(pathname)s#%(lineno)d [%(thread)04d]\n%(message)s',
                    datefmt='%H:%M:%S')        
        self.log = logging.getLogger('picard')
        
        QtCore.QObject.tagger = self
        QtCore.QObject.config = self.config
        QtCore.QObject.log = self.log

        self.setupGettext(localeDir)
        self.loadComponents()

        self.worker = WorkerThread()
        self.connect(self.worker, QtCore.SIGNAL("addFiles(const QStringList &)"), self.onAddFiles)
        
        self.browserIntegration = BrowserIntegration()
        
        self.fileManager = FileManager()

        self.clusters = []
        self.unmatchedFiles = Cluster(_(u"Unmatched Files"))

        self.albums = []

        self.connect(self.browserIntegration, QtCore.SIGNAL("loadAlbum(const QString &)"), self.loadAlbum)
        
        self.window = MainWindow()
        self.connect(self.window, QtCore.SIGNAL("addFiles"), self.onAddFiles)
        self.connect(self.window, QtCore.SIGNAL("addDirectory"), self.onAddDirectory)
        self.connect(self.worker, QtCore.SIGNAL("statusBarMessage(const QString &)"), self.window.setStatusBarMessage)
        self.connect(self.window, QtCore.SIGNAL("search"), self.onSearch)
        self.connect(self.window, QtCore.SIGNAL("lookup"), self.onLookup)
        
        self.worker.start()
        self.browserIntegration.start()
        
    def exit(self):
        self.browserIntegration.stop()
        self.worker.stop()
        
    def run(self):
        self.window.show()
        res = self.exec_()
        self.exit()
        return res
        
    def setupGettext(self, localeDir):
        """Setup locales, load translations, install gettext functions."""
        if sys.platform == "win32":
            try:
                locale.setlocale(locale.LC_ALL, os.environ["LANG"])
            except KeyError:    
                os.environ["LANG"] = locale.getdefaultlocale()[0]
                locale.setlocale(locale.LC_ALL, "")
            except:
                pass
        else:
            try:
                locale.setlocale(locale.LC_ALL, "")
            except:
                pass

        try:
            self.log.debug("Loading gettext translation, localeDir=%r", localeDir)
            self.translation = gettext.translation("picard", localeDir)
            self.translation.install(True)
        except IOError, e:
            __builtin__.__dict__['_'] = lambda a: a 
            self.log.warning(e)

    def loadComponents(self):
        # Load default components
        default_components = (
            'picard.plugins.mutagenmp3',
            'picard.plugins.cuesheet',
            'picard.plugins.csv_opener',
            )
        for module in default_components:
            __import__(module)
            
    def getSupportedFormats(self):
        """Returns list of supported formats.
        
        Format:
            [('.mp3', 'MPEG Layer-3 File'), ('.cue', 'Cuesheet'), ...]
        """
        formats = []
        for opener in self.fileOpeners:
            formats.extend(opener.getSupportedFormats())
        return formats

    def onAddFiles(self, files):
        files = [os.path.normpath(unicode(a)) for a in files]
        self.log.debug("onAddFiles(%r)", files)
        for fileName in files:
            for opener in self.fileOpeners:
                if opener.canOpenFile(fileName):
                    self.worker.readFile(fileName, opener.openFile)
        
    def onAddDirectory(self, directory):
        directory = os.path.normpath(directory)
        self.log.debug("onAddDirectory(%r)", directory)
        self.worker.readDirectory(directory)

    def onSearch(self, text, type_):
        lookup = FileLookup(self, "musicbrainz.org", 80, self.browserIntegration.port)
        getattr(lookup, type_ + "Search")(text)

    def onLookup(self, metadata):
        lookup = FileLookup(self, "musicbrainz.org", 80, self.browserIntegration.port)
        lookup.tagLookup(
            metadata["artist"],
            metadata["album"],
            metadata["title"],
            metadata["tracknumber"],
            str(metadata.get("~#length", 0)),
            metadata["~filename"],
            metadata["musicip_puid"])
        
    def saveFiles(self, files):
        for file in files:
            self.worker.saveFile(file)

    # Albums
    
    def loadAlbum(self, albumId):
        album = Album(unicode(albumId), "[loading album information]", None)
        self.albums.append(album)
        self.connect(album, QtCore.SIGNAL("trackUpdated"), self, QtCore.SIGNAL("trackUpdated"))
        self.emit(QtCore.SIGNAL("albumAdded"), album)
        self.worker.loadAlbum(album)

    def getAlbumById(self, albumId):
        for album in self.albums:
            if album.id == albumId:
                return album
        return None

    def removeAlbum(self, album):
        # Move all linked files to "Unmatched Files"
        for track in album.tracks:
            if track.isLinked():
                file = track.getLinkedFile()
                file.moveToCluster(self.unmatchedFiles)
        # Remove the album
        index = self.albums.index(album)
        del self.albums[index]
        self.emit(QtCore.SIGNAL("albumRemoved"), album, index)

    # Auto-tagging

    def autoTag(self, files):
        # If the user selected no or only one file, use all unmatched files
        if len(files) <= 1:
            files = self.fileManager.files.values()
            
        self.log.debug("Auto-tagging started... %r", files)
        
        # Do metadata lookups for all files
        q = Query()
        for file in files:
            flt = TrackFilter(title=file.metadata["title"],
                artistName=file.metadata["artist"],
                releaseTitle=file.metadata["album"],
                duration=file.metadata.get("~#length", 0),
                limit=5)
            tracks = q.getTracks(filter=flt)
            file.matches = [tr.track for tr in tracks]

        # Get list of releases used in matches
        releases = {}
        for file in files:
            for track in file.matches:
                for release in track.releases:
                    try:
                        releases[release.id] += 1
                    except KeyError:
                        releases[release.id] = 1

        # Sort releases by usage, load the most used one
        if releases:
            releases = releases.items()
            releases.sort(lambda a, b: b[1] - a[1])
            self.loadAlbum(releases[0][0])

def main(localeDir=None):
    try:
        import psyco
        psyco.profile()    
    except ImportError:
        pass
    tagger = Tagger(localeDir)
    sys.exit(tagger.run())

