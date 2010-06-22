# ***** BEGIN LICENSE BLOCK *****
# Version: MPL 1.1/GPL 2.0/LGPL 2.1
# 
# The contents of this file are subject to the Mozilla Public License
# Version 1.1 (the "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of the License at
# http://www.mozilla.org/MPL/
# 
# Software distributed under the License is distributed on an "AS IS"
# basis, WITHOUT WARRANTY OF ANY KIND, either express or implied. See the
# License for the specific language governing rights and limitations
# under the License.
# 
# The Original Code is Komodo code.
# 
# The Initial Developer of the Original Code is ActiveState Software Inc.
# Portions created by ActiveState Software Inc are Copyright (C) 2000-2010
# ActiveState Software Inc. All Rights Reserved.
# 
# Contributor(s):
#   ActiveState Software Inc
# 
# Alternatively, the contents of this file may be used under the terms of
# either the GNU General Public License Version 2 or later (the "GPL"), or
# the GNU Lesser General Public License Version 2.1 or later (the "LGPL"),
# in which case the provisions of the GPL or the LGPL are applicable instead
# of those above. If you wish to allow use of your version of this file only
# under the terms of either the GPL or the LGPL, and not to allow others to
# use your version of this file under the terms of the MPL, indicate your
# decision by deleting the provisions above and replace them with the notice
# and other provisions required by the GPL or the LGPL. If you do not delete
# the provisions above, a recipient may use your version of this file under
# the terms of any one of the MPL, the GPL or the LGPL.
# 
# ***** END LICENSE BLOCK *****

from xpcom import components, ServerException, nsError
from xpcom.server import WrapObject, UnwrapObject
 
import json, sys, os, re
from os.path import join
from koTreeView import TreeView

import copy
import eollib
import fileutils
import koToolbox2
from projectUtils import *
import fileutils
import shutil

import logging

log = logging.getLogger("Toolbox2HTreeView")
#log.setLevel(logging.DEBUG)

_tbdbSvc = None  # module-global handle to the database service
_view = None     # module-global handle to the tree-view (needs refactoring)

"""
Manage a hierarchical view of the loaded tools.  The hierarchy stores
only the name, id, iconurl of a tool, and a list of filtered child nodes by id
"""

class _KoToolHView(object):
    isContainer = False
    def __init__(self, tool):
        self.id = tool.id
        self.name = tool.name
        self.iconurl = tool.get_iconurl()
        self.path = tool.get_path()

    def get_type(self):
        return self.typeName
    
    def get_id(self):
        return str(self.id)

    def get_name(self):
        return self.name

    def get_iconurl(self):
        return self.iconurl
            
class _KoContainerHView(_KoToolHView):
    isContainer = True
    folderTypes = ('folder', 'menu', 'toolbar')
    def rebuildChildren(self):
        self.unfilteredChildNodes = [x + (x[2] in self.folderTypes,)
                                     for x in _tbdbSvc.getChildNodes(self.id)]

    def addChild(self, item):
        item_uw = UnwrapObject(item)
        id = int(item_uw.id)
        self.unfilteredChildNodes.append((id, item_uw.name, item_uw.typeName, item_uw.isContainer))
        #self.childIDs.append(id)

    def removeChild(self, childViewItem):
        child_id = int(childViewItem.id)
        for i, node in enumerate(self.unfilteredChildNodes):
            if node[0] == child_id:
                del self.unfilteredChildNodes[i]
                break
        else:
            #log.debug("Failed to find a child node in parent %d", self.id)
            pass
        
    def __init__(self, tool):
        _KoToolHView.__init__(self, tool)
        self.rebuildChildren()
        self.isOpen = False

class _KoFolderHView(_KoContainerHView):
    typeName = 'folder'

    def getImageSrc(self, index, column):
        if self.level > 0:
            return self.get_iconurl()
        # Now we need to check to see if we're in the std toolbox range,
        # or elsewhere
        nextToolbox = _view.getNextSiblingIndexModel(0)
        if nextToolbox == -1 or index < nextToolbox - 1:
            return self.get_iconurl()
        else:
            return 'chrome://fugue/skin/icons/toolbox.png'

class _KoComplexContainerHView(_KoFolderHView):
    pass

class _KoMenuHView(_KoComplexContainerHView):
    typeName = 'menu'
        
class _KoToolbarHView(_KoComplexContainerHView):
    typeName = 'toolbar'

class _KoCommandToolHView(_KoToolHView):
    typeName = 'command'

class _KoURL_LikeToolHView(_KoToolHView):
    pass

class _KoMacroToolHView(_KoToolHView):
    typeName = 'macro'

class _KoSnippetToolHView(_KoToolHView):
    typeName = 'snippet'

class _KoTemplateToolHView(_KoURL_LikeToolHView):
    typeName = 'template'

class _KoURLToolHView(_KoURL_LikeToolHView):
    typeName = 'URL'

class KoToolbox2HTreeView(TreeView):
    _com_interfaces_ = [components.interfaces.nsIObserver,
                        components.interfaces.koIToolbox2HTreeView,
                        components.interfaces.nsITreeView]
    _reg_clsid_ = "{7b345b58-bae7-4e0b-9a49-30119a1ffd29}"
    _reg_contractid_ = "@activestate.com/KoToolbox2HTreeView;1"
    _reg_desc_ = "KoToolbox2 Hierarchical TreeView"

    _SORT_BY_NATURAL_ORDER = components.interfaces.koIToolbox2HTreeView.SORT_BY_NATURAL_ORDER
    _SORT_BY_NAME_ASCENDING = components.interfaces.koIToolbox2HTreeView.SORT_BY_NAME_ASCENDING
    _SORT_BY_NAME_DESCENDING = components.interfaces.koIToolbox2HTreeView.SORT_BY_NAME_DESCENDING

    def __init__(self, debug=None):
        TreeView.__init__(self, debug=0)
        # The _rows_model shows all the rows currently loaded
        # The _rows_view shows all the rows actually displayed
        # _rows_view == _rows_model[1:] 
        self._rows_model = []
        self._rows_view = []
        self._sortDirection = self._SORT_BY_NATURAL_ORDER
        self._nodeOpenStatusFromName = {}
        self._tree = None
        self.toolbox_db = None
        _observerSvc = components.classes["@mozilla.org/observer-service;1"].\
            getService(components.interfaces.nsIObserverService)
        _observerSvc.addObserver(self, 'toolbox-tree-changed', 0)
        _observerSvc.addObserver(self, 'tool-appearance-changed', 0)
        _observerSvc.addObserver(self, 'xpcom-shutdown', 0)
        self.toolbox2Svc = components.classes["@activestate.com/koToolBox2Service;1"]\
                .getService(components.interfaces.koIToolBox2Service)

        self._unfilteredRows_view = self._unfilteredRows_model = None
        self._toolsMgr = UnwrapObject(components.classes["@activestate.com/koToolbox2ToolManager;1"].getService(components.interfaces.koIToolbox2ToolManager))
        
    def initialize(self):
        prefs = components.classes["@activestate.com/koPrefService;1"].\
            getService(components.interfaces.koIPrefService).prefs
        if not prefs.hasPref("toolbox2"):
            toolboxPrefs = components.classes["@activestate.com/koPreferenceSet;1"].createInstance()
            prefs.setPref("toolbox2", toolboxPrefs)
        else:
            toolboxPrefs = prefs.getPref("toolbox2")
        if toolboxPrefs.hasPref("sortDirection"):
            sortDirectionString = toolboxPrefs.getStringPref("sortDirection")
            self._sortDirection = {'natural':self._SORT_BY_NATURAL_ORDER,
                                   'ascending':self._SORT_BY_NAME_ASCENDING,
                                   'descending':self._SORT_BY_NAME_DESCENDING,
                                   }.get(sortDirectionString,
                                         self._SORT_BY_NATURAL_ORDER)
                                   
            
        if toolboxPrefs.hasPref("open-nodes"):
            self._nodeOpenStatusFromName = json.loads(toolboxPrefs.getStringPref("open-nodes"))
        else:
            self._nodeOpenStatusFromName = {}
        global _tbdbSvc, _view
        _tbdbSvc = self.toolbox_db = UnwrapObject(components.classes["@activestate.com/KoToolboxDatabaseService;1"].\
                       getService(components.interfaces.koIToolboxDatabaseService))
        _view = self
        self._toolsManager = UnwrapObject(components.classes["@activestate.com/koToolbox2ToolManager;1"]
        .getService(components.interfaces.koIToolbox2ToolManager));
        self._std_toolbox_id = self.toolbox2Svc.getStandardToolboxID()

        self._redoTreeView()
        self._restoreView()
        self._tree.invalidate()

    def observe(self, subject, topic, data):
        if not topic:
            return
        if topic == 'toolbox-tree-changed':
            self._redoTreeView()
        elif topic == 'tool-appearance-changed':
            # Update the tool's values, and then invalidate the row
            id = int(data)
            index = self.getIndexById(id)
            if index == -1:
                return
            node = self._rows_view[index]
            tool = self._toolsMgr.getToolById(id)
            node.name = tool.name
            node.iconurl = tool.get_iconurl()
            self._tree.invalidateRow(index)
            #TODO: Get the parent, refilter and resort its nodes, and redisplay.
        elif topic == 'xpcom-shutdown':
            _observerSvc = components.classes["@mozilla.org/observer-service;1"].\
                getService(components.interfaces.nsIObserverService)
            _observerSvc.removeObserver(self, 'toolbox-tree-changed')
            _observerSvc.removeObserver(self, 'xpcom-shutdown')

    def getPathFromIndex(self, index):
        id = self._rows_view[index].id
        return self.toolbox_db.getPath(id)

    def get_toolType(self, index):
        if index == -1:
            return None
        return self._rows_view[index].typeName
    
    def getIndexByPath(self, path):
        for i, row in enumerate(self._rows_view):
            if row.path == path:
                return i
        return -1
    
    def getIndexByPathModel(self, path, viewIndex=None):
        if viewIndex is not None:
            if self._rows_model[viewIndex].path == path:
                return viewIndex
            elif (viewIndex > 0
                  and self._rows_model[viewIndex - 1].path == path):
                return viewIndex - 1
        for i, row in enumerate(self._rows_model):
            if row.path == path:
                return i
        return -1
    
    def getIndexByTool(self, tool):
        path = tool.path
        for i, row in enumerate(self._rows_view):
            #log.debug("%d: %s (%s/%s", i, row.path, row.typeName, row.name)
            if row.path == path:
                return i
        return -1

    def getIndexByToolFromModel(self, tool):
        path = tool.path
        for i, row in enumerate(self._rows_model):
            #log.debug("%d: %s (%s/%s", i, row.path, row.typeName, row.name)
            if row.path == path:
                return i
        return -1

    def getIndexById(self, id):
        for i, row in enumerate(self._rows_view):
            #log.debug("%d: %d (%s/%s", i, row.id, row.typeName, row.name)
            if row.id == id:
                return i
        return -1

    def selectedItemsHaveSameParent(self):
        # Ignore all child nodes in the selection
        treeSelection = self.selection
        selectedIndices = []
        parent_index = -2
        rangeCount = treeSelection.getRangeCount()
        for i in range(rangeCount):
            min_index, max_index = treeSelection.getRangeAt(i)
            index = min_index
            while index < max_index + 1:
                tool = self.getTool(index)
                res = self.toolbox_db.getValuesFromTableByKey('hierarchy',
                                                      ['parent_path_id'],
                                                      'path_id', tool.id)
                if not res:
                    if parent_index != -2:
                        return False
                else:
                    candidate_index = res[0]
                    if parent_index == -2:
                        parent_index = candidate_index
                    elif parent_index != candidate_index:
                        return False
                # And skip any selected children, if all are selected.
                if self.isContainerOpen(index):
                    nextSiblingIndex = self.getNextSiblingIndex(index)
                    if nextSiblingIndex <= max_index + 1:
                        index = nextSiblingIndex
                        continue  # don't increment at end of loop
                    elif (nextSiblingIndex == -1
                          and max_index == len(self._rows_view) - 1
                          and i == rangeCount - 1):
                        return True
                    else:
                        return False
                
                index += 1 # end while index < max_index + 1:
        return True

    # Is the node at row srcIndex an ancestor of the node at targetIndex?
    def isAncestor(self, srcIndex, targetIndex):
        if srcIndex < 0 or targetIndex < 0:
            return False
        elif srcIndex > targetIndex:
            return False
        elif srcIndex == targetIndex:
            return True
        
        srcLevel = self._rows_view[srcIndex].level
        while targetIndex > srcIndex and self._rows_view[targetIndex].level > srcLevel:
            targetIndex -= 1
        return targetIndex == srcIndex

    def addNewItemToParent(self, parent, item):
        # Not XPCOM
        # Called from the model.
        # Add and show the new item
        # Work on the "model view", and then refilter into the
        # actual view, because we might be adding items to the
        # invisible toolbox.
        self._toolsMgr.addNewItemToParent(parent, item, showNewItem=False)
        index = self.getIndexByToolFromModel(parent)
        if index == -1:
            raise Exception(nsError.NS_ERROR_ILLEGAL_VALUE,
                                  ("Can't find parent %s/%s in the tree" %
                                   (parent.type, parent.name)))
        view_before_len = len(self._rows_view)
        self._rows_model[index].addChild(item)
        firstVisibleRow = self._tree.getFirstVisibleRow()
        if self.isContainerOpenModel(index):
            #TODO: Make showing the added item a pref?
            # Easy hack to resort the items
            self.toggleOpenStateModel(index)
            self.toggleOpenStateModel(index)
        else:
            self.toggleOpenStateModel(index)
        self._filter_std_toolbox()
        try:
            newIndex = self._rows_view.index(item, index - 1)
        except ValueError:
            pass
        else:
            index = newIndex
        view_after_len = len(self._rows_view)
        self._tree.rowCountChanged(0, view_after_len - view_before_len)
        self._tree.scrollToRow(firstVisibleRow)
        self.selection.currentIndex = index
        self.selection.select(index)
        self._tree.ensureRowIsVisible(index)

    def deleteToolAt(self, index, path=None):
        row = self._rows_view[index]
        if path is None:
            path = row.path
        self._toolsMgr.deleteTool(row.id)

        parentIndex = self.getParentIndex(index)
        if parentIndex > -1:
            self._rows_view[parentIndex].removeChild(row)

        # Now update the view.
        self._tree.beginUpdateBatch()
        try:
            if self.isContainerOpen(index):
                self.toggleOpenState(index)
            try:
                del self._nodeOpenStatusFromName[path]
            except KeyError:
                pass
            del self._rows_view[index]
            del self._rows_model[index - 1]
            self._tree.rowCountChanged(index, -1)
        finally:
            self._tree.endUpdateBatch()

    def copyLocalFolder(self, srcPath, targetDirPath):
        fileutils.copyLocalFolder(srcPath, targetDirPath)

    def pasteItemsIntoTarget(self, targetIndex, paths, copying):
        # We need to work with the model indices, because we're
        # changing this tree.  We can use the current view to
        # find the source and destination, but then need to work
        # with model coordinates for updating the rows.
        targetTool = self.getTool(targetIndex)
        if not targetTool.isContainer:
            raise Exception("pasteItemsIntoTarget: item at row %d isn't a container, it's a %s" %
                            (targetIndex, targetTool.typeName))
        targetPath = self.getPathFromIndex(targetIndex)
        targetId = self.toolbox_db.get_id_from_path(targetPath)
        if targetId is None:
            raise Exception("target %s (%d) isn't in the database" % (
                targetPath, targetIndex
            ))
        if copying:
            # Just copy the paths to the targetIndex, refresh the
            # target and its children, and refresh the view
            for path in paths:
                if not os.path.exists(path):
                    #TODO: Bundle all the problems into one string that gets raised back.
                    log.debug("Path %s doesn't exist", path)
                elif os.path.isdir(path):
                    if targetPath.startswith(path):
                        log.error("Refuse to copy path %s into one of its descendants (%s)",
                                  path, targetPath)
                        continue
                    try:
                        fileutils.copyLocalFolder(path, targetPath)
                    except:
                        log.exception("fileutils.copyLocalFolder(src:%s to  targetPath:%s) failed", path, targetPath)
                else:
                    try:
                        shutil.copy(path, targetPath)
                    except:
                        log.exception("Can't copy src:%s to  targetPath:%s", path, targetPath)
            self.toolbox2Svc.reloadToolsDirectory(targetPath)
            self.reloadToolsDirectoryView(targetIndex)
            return

        parentIndicesToUpdate = [targetIndex]
        # Moving is harder, because we have to track the indices we've dropped.
        for path in paths:
            if not os.path.exists(path):
                #TODO: Bundle all the problems into one string that gets raised back.
                log.debug("Path %s doesn't exist", path)
                continue
            if os.path.isdir(path):
                if targetPath.startswith(path):
                    log.error("Refuse to move path %s into one of its descendants (%s)",
                              path, targetPath)
                    continue
                try:
                    fileutils.copyLocalFolder(path, targetPath)
                    shutil.rmtree(path)
                except:
                    log.exception("fileutils.copyLocalFolder(src:%s to  targetPath:%s) failed", path, targetPath)
                    continue
            else:
                try:
                    finalTargetPath = join(targetPath, os.path.basename(path))
                    shutil.move(path, finalTargetPath)
                except:
                    log.exception("shutil.move(src:%s to finalTargetPath:%s) failed", path, finalTargetPath)
                    continue
            index = self.getIndexByPath(path)
            parentIndex = self.getParentIndex(index)
            if parentIndex not in parentIndicesToUpdate:
                parentIndicesToUpdate.append(parentIndex)
                
        self._tree.beginUpdateBatch()
        try:
            parentIndicesToUpdate.sort(reverse=True)
            for parentIndex in parentIndicesToUpdate:
                parentPath = self.getPathFromIndex(parentIndex)
                self.toolbox2Svc.reloadToolsDirectory(parentPath)
                self.reloadToolsDirectoryView(parentIndex)
        finally:
                self._tree.endUpdateBatch()
        #self.refreshFullView() #TODO: refresh only parentIndicesToUpdate
        finalTargetIndex = self.getIndexByPath(targetPath)
        self._tree.ensureRowIsVisible(finalTargetIndex)
                
    def reloadToolsDirectoryView(self, viewIndex):
        # Refresh the model tree, and refilter into the view tree
        before_len = len(self._rows_view)
        targetPath = self.getPathFromIndex(viewIndex)
        modelIndex = self.getIndexByPathModel(targetPath, viewIndex)
        node = self._rows_model[modelIndex]
        if node.isContainer:
            node.rebuildChildren()
        self.refreshView_Model(modelIndex)
        self._filter_std_toolbox()
        after_len = len(self._rows_view)
        self._tree.rowCountChanged(0, after_len - before_len)

    def _zipNode(self, zf, currentDirectory):
        nodes = os.listdir(currentDirectory)
        numZippedItems = 0
        for path in nodes:
            fullPath = join(currentDirectory, path)
            # these filenames should be "sluggified" already,
            # although maybe not the dirnames.
            relativePath = fullPath[self._targetZipFileRootLen:]
            if os.path.isfile(fullPath):
                zf.write(fullPath, relativePath)
                numZippedItems += 1
            elif os.path.isdir(fullPath) and not os.path.islink(fullPath):
                numZippedItems += self._zipNode(zf, fullPath)
        return numZippedItems

    def zipSelectionToFile(self, targetZipFile):
        selectedIndices = self.getSelectedIndices(rootsOnly=True)
        import zipfile
        zf = zipfile.ZipFile(targetZipFile, 'w')
        numZippedItems = 0
        for index in selectedIndices:
            tool = self.getTool(index)
            path = self.getPathFromIndex(index)
            if tool.isContainer and path[-1] in "\\/":
                path = path[:-1]
            self._targetZipFileRootLen = len(os.path.dirname(path)) + 1
            if not tool.isContainer:
                zf.write(path, path[self._targetZipFileRootLen:])
                numZippedItems += 1
            else:
                numZippedItems += self._zipNode(zf, path)
        return numZippedItems

    def getSelectedIndices(self, rootsOnly=False):
        treeSelection = self.selection
        selectedIndices = []
        numRanges = treeSelection.getRangeCount()
        for i in range(numRanges):
            min_index, max_index = treeSelection.getRangeAt(i)
            index = min_index
            while index < max_index + 1:
                selectedIndices.append(index)
                if rootsOnly and self.isContainerOpen(index):
                    nextSiblingIndex = self.getNextSiblingIndex(index)
                    if nextSiblingIndex <= max_index + 1:
                        index = nextSiblingIndex - 1
                    else:
                        if nextSiblingIndex == -1 and i < numRanges - 1:
                            raise ServerException(nsError.NS_ERROR_ILLEGAL_VALUE,
                              ("node at row %d supposedly at end, but we're only at range %d of %d" %
                               (j, i + 1, numRanges)))
                        index = max_index
                index += 1
        return selectedIndices
    
    def refreshFullView(self):
        i = 0
        lim = len(self._rows_model)
        view_before_len = len(self._rows_view)
        std_toolbox_id = self.toolbox2Svc.getStandardToolboxID()
        firstVisibleRow = self._tree.getFirstVisibleRow()
        currentIndex = self.selection.currentIndex;
        while i < lim:
            before_len = len(self._rows_model)
            if self.isContainerOpenModel(i):
                self.toggleOpenStateModel(i)
                self.toggleOpenStateModel(i)
            elif (self._nodeOpenStatusFromName.get(self._rows_model[i].path, False)
                  or self._rows_model[i].id == std_toolbox_id):
                # Force the stdtoolbox open
                self.toggleOpenStateModel(i)
            after_len = len(self._rows_model)
            delta = after_len - before_len
            lim += delta
            i += delta + 1
        self._filter_std_toolbox()
        self._tree.ensureRowIsVisible(firstVisibleRow)
        self.selection.select(currentIndex)
        view_after_len = len(self._rows_view)
        self._tree.rowCountChanged(0, view_after_len - view_before_len)
        self._tree.invalidate()
            
    def refreshView(self, index):
        firstVisibleRow = self._tree.getFirstVisibleRow()
        before_len = len(self._rows_view)
        modelIndex = self._modelIndexFromViewIndex(index)
        if self.isContainerOpenModel(index):
            self.toggleOpenStateModel(modelIndex)
            self.toggleOpenStateModel(modelIndex)
        elif self._nodeOpenStatusFromName.get(self._rows_view[index].path, None):
            self.toggleOpenStateModel(modelIndex)
        self._filter_std_toolbox()
        after_len = len(self._rows_view)
        delta = after_len - before_len
        if delta:
            self._tree.rowCountChanged(index, delta)
        self._tree.ensureRowIsVisible(firstVisibleRow)

    def _modelIndexFromViewIndex(self, viewIndex):
        path = self._rows_view[viewIndex].path
        if self._rows_model[viewIndex].path == path:
            return viewIndex
        elif self._rows_model[viewIndex + 1].path == path:
            return viewIndex + 1
        else:
            modelIndex = self.getIndexByPathModel(path)
            return modelIndex        

    def refreshView_Model(self, index):
        if self.isContainerOpenModel(index):
            self.toggleOpenStateModel(index)
            self.toggleOpenStateModel(index)
        elif self._nodeOpenStatusFromName.get(self._rows_model[index].path, None):
            # Force it open.
            self.toggleOpenStateModel(index)

    def _redoTreeView(self):
        self._tree.beginUpdateBatch()
        try:
            self._redoTreeView1_aux()
        finally:
            pass
            self._tree.endUpdateBatch()
        self.refreshFullView()

    def _redoTreeView1_aux(self):
        top_level_nodes = self.toolbox_db.getTopLevelNodes()
        top_level_ids = [x[0] for x in top_level_nodes]
        index = 0
        lim = len(self._rows_model)
        while index < lim:
            id = int(self._rows_model[index].id)
            nextIndex = self.getNextSiblingIndexModel(index)
            if nextIndex == -1:
                finalIndex = lim
            else:
                finalIndex = nextIndex
            if id in top_level_ids:
                del top_level_ids[top_level_ids.index(id)]
                index = finalIndex
            else:
                if nextIndex == -1:
                    del self._rows_model[index - 1:]
                else:
                    del self._rows_model[index - 1: nextIndex - 1]
        
        for path_id, name, node_type in top_level_nodes:
            if path_id not in top_level_ids:
                #log.debug("No need to reload tree %s", name)
                continue
            toolPart = self._toolsManager.getOrCreateTool(node_type, name, path_id)
            toolView = createToolViewFromTool(toolPart) 
            toolView.level = 0
            self._rows_model.append(toolView)
        
    def _restoreView(self):

        toolboxPrefs = components.classes["@activestate.com/koPrefService;1"].\
            getService(components.interfaces.koIPrefService).prefs.getPref("toolbox2")
        if toolboxPrefs.hasPref("firstVisibleRow"):
            firstVisibleRow = toolboxPrefs.getLongPref("firstVisibleRow")
        else:
            firstVisibleRow = -1
        if toolboxPrefs.hasPref("currentIndex"):
            currentIndex = toolboxPrefs.getLongPref("currentIndex")
        else:
            currentIndex = -1

        self._restoreViewWithSettings(firstVisibleRow, currentIndex)

    def _restoreViewWithSettings(self, firstVisibleRow, currentIndex):
        greatestPossibleFirstVisibleRow = len(self._rows_view) - self._tree.getPageLength()
        if greatestPossibleFirstVisibleRow < 0:
            greatestPossibleFirstVisibleRow = 0
        if firstVisibleRow > greatestPossibleFirstVisibleRow:
            firstVisibleRow = greatestPossibleFirstVisibleRow
        
        if currentIndex >= len(self._rows_view):
           currentIndex =  len(self._rows_view) - 1

        if firstVisibleRow != -1:
            self._tree.scrollToRow(firstVisibleRow)
        if currentIndex != -1:
            self.selection.currentIndex = currentIndex
            self.selection.select(currentIndex)
            self._tree.ensureRowIsVisible(currentIndex)

    def terminate(self):
        prefs = components.classes["@activestate.com/koPrefService;1"].\
            getService(components.interfaces.koIPrefService).prefs
        try:
            toolboxPrefs = prefs.getPref("toolbox2")
            toolboxPrefs.setStringPref("open-nodes",
                                       json.dumps(self._nodeOpenStatusFromName))
            toolboxPrefs.setLongPref("firstVisibleRow",
                                     self._tree.getFirstVisibleRow())
            toolboxPrefs.setLongPref("currentIndex",
                                     self.selection.currentIndex)
        except:
            log.exception("problem in terminate")

    def getTool(self, index):
        if index < 0: return None
        try:
            id = self._rows_view[index].id
            return self._toolsManager.getToolById(id)
        except IndexError:
            log.error("Failed getTool(index:%d), id:%r", index, id)
            return None

    def get_type(self, index):
        if index == -1:
            return None
        return self._rows_view[index].typeName
        
    #---- nsITreeView Methods
    
    def get_rowCount(self):
        return len(self._rows_view)

    def getCellText(self, index, column):
        col_id = column.id
        assert col_id == "Name"
        #log.debug(">> getCellText:%d, %s", row, col_id)
        try:
            return self._rows_view[index].name
        except AttributeError:
            log.debug("getCellText: No id %s at row %d", col_id, row)
            return "?"
        
    def getImageSrc(self, index, column):
        col_id = column.id
        assert col_id == "Name"
        node = self._rows_view[index]
        method = getattr(node, "getImageSrc", None)
        if method:
            return method(index, column)
        try:
            return self._rows_view[index].get_iconurl()
        except:
            return ""
        
    def isContainer(self, index):
        try:
            return self._rows_view[index].isContainer
        except IndexError:
            log.error("isContainer[index:%d]", index)
            return False
        
    def isContainerOpen(self, index):
        node = self._rows_view[index]
        return node.isContainer and node.isOpen
        
    def isContainerOpenModel(self, index):
        node = self._rows_model[index]
        return node.isContainer and node.isOpen
        
    def isContainerEmpty(self, index):
        node = self._rows_view[index]
        return node.isContainer and not node.unfilteredChildNodes

    def getParentIndex(self, index):
        if index >= len(self._rows_view) or index < 0: return -1
        try:
            i = index - 1
            level = self._rows_view[index].level
            while i >= 0 and self._rows_view[i].level >= level:
                i -= 1
        except IndexError:
            i = -1
        return i

    def hasNextSibling(self, index, afterIndex):
        if index >= len(self._rows_view) or index < 0: return 0
        try:
            current_level = self._rows_view[index].level
            for next_row in self._rows_view[afterIndex + 1:]:
                next_row_level = next_row.level
                if next_row_level < current_level:
                    return 0
                elif next_row_level == current_level:
                    return 1
        except IndexError:
            pass
        return 0
    
    def getLevel(self, index):
        try:
            return self._rows_view[index].level
        except IndexError:
            return -1
                                                
    def setTree(self, tree):
        self._tree = tree
        
    def getNextSiblingIndex(self, index):
        """
        @param index {int} points to the node whose next-sibling we want to find.
        @return index of the sibling, or -1 if not found.
        """
        level = self._rows_view[index].level
        lim = len(self._rows_view)
        index += 1
        while index < lim:
            if self._rows_view[index].level <= level:
                return index
            index += 1
        return -1
    
    def getNextSiblingIndexModel(self, index):
        """
        @param index {int} points to the node whose next-sibling we want to find.
        @return index of the sibling, or -1 if not found.
        """
        level = self._rows_model[index].level
        node = self._rows_model[index]
        lim = len(self._rows_model)
        index += 1
        while index < lim:
            if self._rows_model[index].level <= level:
                return index
            index += 1
        return -1

    def setFilter(self, filterPattern):
        if not filterPattern:
            self.clearFilter()
        else:
            self.useFilter(filterPattern)
            
    def clearFilter(self):
        currentIndex = -1
        # Set currentIndex to the current node if it's selected,
        # and it's a container -- the idea is to keep the container around,
        if self.selection.count > 0:
            index = self.selection.currentIndex
            if self.isContainer(index):
                pathsToOpen = []
                currentIndex = index
                currentPath = self._rows_view[index].path
                while True:
                    parentIndex = self.getParentIndex(index)
                    if parentIndex == -1 or parentIndex == index:
                        break
                    index = parentIndex
                    rowNode = self._rows_view[index]
                    path = rowNode.path
                    if path in self._nodeOpenStatusFromName:
                        break
                    pathsToOpen.append(path)
        before_len = len(self._rows_view)
        self._rows_view = self._unfilteredRows_view
        self._rows_model = self._unfilteredRows_model
        self._unfilteredRows_view = self._unfilteredRows_model = None
        #log.debug("Had %d rows, now have %d rows", before_len, after_len)
        if currentIndex != -1:
            # Open up the necessary nodes first, from the highest
            # nodes first, which happen to be the last ones we
            # pushed on the list.
            # Work with the model nodes, not the filtered view nodes.
            while pathsToOpen:
                path = pathsToOpen.pop()
                candidateIndex = self.getIndexByPathModel(path)
                if not self._rows_model[candidateIndex].isOpen:
                    self._doContainerOpenModel(self._rows_model[candidateIndex],
                                               candidateIndex)
            # Revise: currentIndex to point to new location of currentPath
            currentIndex = self.getIndexByPath(currentPath)
        self._filter_std_toolbox()
        after_len = len(self._rows_view)
        self._tree.rowCountChanged(0, after_len - before_len)
        self._tree.invalidate()
        if currentIndex == -1:
            fvr = self._unfiltered_firstVisibleRow
            ufci = self._unfiltered_currentIndex
        else:
            fvr = -1
            ufci = currentIndex
        self._restoreViewWithSettings(fvr, ufci)
        
    def useFilter(self, filterPattern):
        if self._unfilteredRows_view is None:
            self._unfilteredRows_view = self._rows_view
            self._unfilteredRows_model = self._rows_model
            self._unfiltered_firstVisibleRow = self._tree.getFirstVisibleRow()
            self._unfiltered_currentIndex = self.selection.currentIndex;
            
        import time
        t1 = time.time()
        matched_nodes = _tbdbSvc.getHierarchyMatch(filterPattern)
        t2 = time.time()
        #log.debug("Time to query %s: %g msec", filterPattern, (t2 - t1) * 1000.0)
        #log.debug("matched nodes: %s", matched_nodes)
        before_len = len(self._rows_view)
        self._rows_model = []
        for node in matched_nodes:
            path_id, name, node_type, matchedPattern, level = node
            toolPart = self._toolsManager.getToolById(path_id)
            toolView = createToolViewFromTool(toolPart) 
            toolView.level = level
            self._rows_model.append(toolView)
        self._filter_std_toolbox()
        after_len = len(self._rows_view)
        self._tree.rowCountChanged(0, after_len - before_len)                
        self._tree.invalidate()
        self._restoreViewWithSettings(0, 0)

    def _filter_std_toolbox(self):
        # Copy self._rows_model to self._rows_view, removing the
        # std toolbox node, and shifting its components down one level.
        # Note that the std toolbox node isn't always the first one
        # in the list.
        if len(self._rows_model) == 0:
            self._rows_view = []
            return
        # Copy, because the view's level is different from the model's.
        self._rows_view = copy.deepcopy(self._rows_model)
        lim = len(self._rows_model)
        startPoint = stopPoint = None
        i = 0
        while i < lim:
            next_toolbox_index = self.getNextSiblingIndexModel(i)
            if next_toolbox_index == -1:
                j = lim
            else:
                j = next_toolbox_index
            if self._rows_model[i].id == self._std_toolbox_id:
                del self._rows_view[i]
                startPoint = i
                stopPoint = j - 1
                break
            i = j
        if startPoint is not None:
            for i in range(startPoint, stopPoint):
                self._rows_view[i].level -= 1

    def get_sortDirection(self):
        return self._sortDirection

    def set_sortDirection(self, value):
        self._sortDirection = value
        self.refreshFullView()

    def toggleOpenState(self, index, suppressUpdate=False):
        if self._unfilteredRows_view:
            # "trying to toggle while searching causes all kinds of grief"
            # - koKPFTreeView.p.py
            # To fix: make row info thinner.
            return

        rowNode = self._rows_model[index]
        if not suppressUpdate:
            firstVisibleRow = self._tree.getFirstVisibleRow()
        before_len = len(self._rows_view)
        self.toggleOpenStateModel(index + 1)
        self._filter_std_toolbox()
        after_len = len(self._rows_view)
        delta = after_len - before_len
        if delta:
            self._tree.rowCountChanged(index, delta)
        if not suppressUpdate:
            self._tree.ensureRowIsVisible(firstVisibleRow)
            self.selection.select(index)

    def toggleOpenStateModel(self, index):
        rowNode = self._rows_model[index]
        if rowNode.isOpen:
            try:
                del self._nodeOpenStatusFromName[rowNode.path]
            except KeyError:
                pass
            nextIndex = self.getNextSiblingIndexModel(index)
            if nextIndex == -1:
                del self._rows_model[index + 1:]
            else:
                del self._rows_model[index + 1: nextIndex]
            rowNode.isOpen = False
        else:
            self._doContainerOpenModel(rowNode, index)
            self._nodeOpenStatusFromName[rowNode.path] = True

    def _compareChildNode(self, item1, item2):
        # Nodes contain (id, name, type, isContainer)
        if self._sortDirection == self._SORT_BY_NATURAL_ORDER:
            folderDiff = cmp(not item1[3], not item2[3])
            if folderDiff:
                return folderDiff
        items = [item1, item2]
        if self._sortDirection == self._SORT_BY_NAME_DESCENDING:
            lowerIndex = 1
            upperIndex = 0
        else:
            lowerIndex = 0
            upperIndex = 1
        return cmp(items[lowerIndex][1].lower(), items[upperIndex][1].lower())

    def _sortAndExtractIDs(self, rowNode):
        if not hasattr(rowNode, 'unfilteredChildNodes'):
            rowNode.rebuildChildren()
        sortedNodes = sorted(rowNode.unfilteredChildNodes,
                             cmp=self._compareChildNode)
        return [x[0] for x in sortedNodes]

    def _doContainerOpenModel(self, rowNode, index):
        childIDs = self._sortAndExtractIDs(rowNode)
        if childIDs:
            posn = index + 1
            #for path_id, name, node_type in childNodes:
            for path_id in childIDs:
                toolPart = self._toolsManager.getToolById(path_id)
                toolView = createToolViewFromTool(toolPart)
                toolView.level = rowNode.level + 1
                self._rows_model.insert(posn, toolView)
                posn += 1
            rowNode.isOpen = True
            # Now open internal nodes working backwards
            lastIndex = index + len(childIDs)
            firstIndex = index
            # Work from bottom up so we don't have to readjust the index.
            for i, row in enumerate(self._rows_model[lastIndex: index: -1]):
                openNode = self._nodeOpenStatusFromName.get(row.path, None)
                if openNode:
                    self._doContainerOpenModel(row, lastIndex - i)
                
_partFactoryMap = {}
for name, value in globals().items():
    if isinstance(value, object) and getattr(value, 'typeName', ''):
        _partFactoryMap[value.typeName] = value

def createToolViewFromTool(tool):
    return _partFactoryMap[tool.typeName](tool)

