#!/usr/bin/env python
# License: GPL v3 Copyright: 2019, Kovid Goyal <kovid at kovidgoyal.net>


import re
import sys
import time
from functools import partial

from qt.core import (
    QAbstractItemView,
    QApplication,
    QEvent,
    QFont,
    QHBoxLayout,
    QIcon,
    QMenu,
    QModelIndex,
    QStandardItem,
    QStandardItemModel,
    QStyle,
    QStyledItemDelegate,
    Qt,
    QToolButton,
    QToolTip,
    QTreeView,
    QTimer,
    QWidget,
    pyqtSignal,
)

from calibre import prints
from calibre.gui2 import error_dialog
from calibre.gui2.gestures import GestureManager
from calibre.gui2.search_box import SearchBox2
from calibre.utils.icu import primary_contains
from calibre.utils.localization import _


class Delegate(QStyledItemDelegate):

    def __init__(self, parent=None):
        QStyledItemDelegate.__init__(self, parent)
        self._last_toc_paint_diag = set()

    def paint(self, painter, option, index):
        QStyledItemDelegate.paint(self, painter, option, index)
        model = index.model()
        if not getattr(model, 'additional_debug', False):
            return
        item = model.itemFromIndex(index)
        if item is None:
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        current = index == self.parent().currentIndex() if self.parent() is not None else False
        if not (selected or current or item.is_being_viewed):
            return
        generation = getattr(model, 'current_toc_generation', 0)
        key = (generation, item.node_id, selected, current, item.is_being_viewed)
        if key in self._last_toc_paint_diag:
            return
        self._last_toc_paint_diag.add(key)
        r = option.rect
        prints(
            'TOC-HILITE-DIAG qt.delegate-paint-toc-node',
            {
                'generation': generation, 'node_id': item.node_id, 'title': item.title,
                'selected': selected, 'current': current, 'being_viewed': item.is_being_viewed,
                'font_bold': option.font.bold(), 'font_italic': option.font.italic(),
                'rect': (r.x(), r.y(), r.width(), r.height()),
            },
            file=sys.stderr,
        )

    def helpEvent(self, ev, view, option, index):
        # Show a tooltip only if the item is truncated
        if not ev or not view:
            return False
        if ev.type() == QEvent.Type.ToolTip:
            rect = view.visualRect(index)
            size = self.sizeHint(option, index)
            if rect.width() < size.width():
                tooltip = index.data(Qt.ItemDataRole.DisplayRole)
                QToolTip.showText(ev.globalPos(), tooltip, view)
                return True
        return QStyledItemDelegate.helpEvent(self, ev, view, option, index)


class TOCView(QTreeView):

    searched = pyqtSignal(object)

    def __init__(self, *args):
        QTreeView.__init__(self, *args)
        self._toc_diag_seq = 0
        self._last_paint_diag_seq = -1
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.delegate = Delegate(self)
        self.setItemDelegate(self.delegate)
        self.setMinimumWidth(80)
        self.header().close()
        self.setMouseTracking(True)
        self.set_style_sheet()
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.context_menu = None
        self.customContextMenuRequested.connect(self.show_context_menu)
        QApplication.instance().palette_changed.connect(self.set_style_sheet, type=Qt.ConnectionType.QueuedConnection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.gesture_manager = GestureManager(self)

    def viewportEvent(self, ev):
        if hasattr(self, 'gesture_manager'):
            ret = self.gesture_manager.handle_event(ev)
            if ret is not None:
                return ret
        return super().viewportEvent(ev)

    def setModel(self, model):
        QTreeView.setModel(self, model)
        model.current_toc_nodes_changed.connect(self.current_toc_nodes_changed, type=Qt.ConnectionType.QueuedConnection)

    def current_toc_nodes_changed(self, ancestors, nodes):
        self._toc_diag_seq += 1
        seq = self._toc_diag_seq
        if ancestors:
            self.auto_expand_indices(ancestors)
        if nodes:
            self.scrollTo(nodes[-1].index())
        if getattr(self.model(), 'additional_debug', False):
            prints(
                'TOC-HILITE-DIAG qt.view-current-toc-nodes',
                {
                    'seq': seq,
                    'node_ids': tuple(node.node_id for node in nodes),
                    'node_titles': tuple(node.title for node in nodes),
                    'current_node_id': nodes[-1].node_id if nodes else None,
                    'snapshot': self.toc_diag_snapshot(),
                },
                file=sys.stderr,
            )
        if getattr(self.model(), 'additional_debug', False):
            QTimer.singleShot(0, partial(self.report_delayed_toc_diag, seq, 'after-event-loop'))
            QTimer.singleShot(50, partial(self.report_delayed_toc_diag, seq, 'after-50ms'))

    def toc_diag_snapshot(self):
        model = self.model()
        sm = self.selectionModel()
        current = self.currentIndex()
        current_item = model.itemFromIndex(current) if model is not None and current.isValid() else None
        selected_ids = []
        if sm is not None:
            for idx in sm.selectedIndexes():
                item = model.itemFromIndex(idx)
                if item is not None:
                    selected_ids.append(item.node_id)
        viewed_ids = tuple(node.node_id for node in model.viewed_nodes()) if model is not None else ()
        rect = self.visualRect(current) if current.isValid() else None
        viewport_size = self.viewport().size()
        scrollbar = self.verticalScrollBar()
        return {
            'time': round(time.monotonic(), 6),
            'view_visible': self.isVisible(),
            'viewport_visible': self.viewport().isVisible(),
            'updates_enabled': self.updatesEnabled(),
            'viewport_updates_enabled': self.viewport().updatesEnabled(),
            'has_focus': self.hasFocus(),
            'current_index_valid': current.isValid(),
            'current_node_id': getattr(current_item, 'node_id', None),
            'selected_node_ids': tuple(selected_ids),
            'viewed_node_ids': viewed_ids,
            'current_visual_rect': (
                rect.x(), rect.y(), rect.width(), rect.height()) if rect is not None and rect.isValid() else None,
            'viewport_size': (viewport_size.width(), viewport_size.height()),
            'vscroll': (scrollbar.value(), scrollbar.minimum(), scrollbar.maximum()) if scrollbar is not None else None,
            'model_leaf_ids': getattr(model, 'current_toc_leaf_ids', ()),
            'model_generation': getattr(model, 'current_toc_generation', 0),
        }

    def report_delayed_toc_diag(self, seq, label):
        if getattr(self.model(), 'additional_debug', False):
            prints(
                'TOC-HILITE-DIAG qt.view-delayed-state',
                {'seq': seq, 'label': label, 'snapshot': self.toc_diag_snapshot()},
                file=sys.stderr,
            )

    def paintEvent(self, ev):
        QTreeView.paintEvent(self, ev)
        if getattr(self.model(), 'additional_debug', False) and self._last_paint_diag_seq != self._toc_diag_seq:
            self._last_paint_diag_seq = self._toc_diag_seq
            prints(
                'TOC-HILITE-DIAG qt.view-paint-event',
                {'seq': self._toc_diag_seq, 'snapshot': self.toc_diag_snapshot()},
                file=sys.stderr,
            )

    def auto_expand_indices(self, indices):
        for idx in indices:
            self.setExpanded(idx, True)

    def set_style_sheet(self):
        self.setStyleSheet('''
            QTreeView {
                background-color: palette(window);
                color: palette(window-text);
                border: none;
            }

            QTreeView::item {
                border: 1px solid transparent;
                padding-top:0.5ex;
                padding-bottom:0.5ex;
            }

        ''' + QApplication.instance().palette_manager.tree_view_hover_style())
        self.setProperty('hovered_item_is_highlighted', True)

    def mouseMoveEvent(self, ev):
        if self.indexAt(ev.pos()).isValid():
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.unsetCursor()
        return QTreeView.mouseMoveEvent(self, ev)

    def expand_tree(self, index):
        self.expand(index)
        i = -1
        while True:
            i += 1
            child = index.child(i, 0)
            if not child.isValid():
                break
            self.expand_tree(child)

    def collapse_at_level(self, index):
        item = self.model().itemFromIndex(index)
        for x in self.model().items_at_depth(item.depth):
            self.collapse(self.model().indexFromItem(x))

    def expand_at_level(self, index):
        item = self.model().itemFromIndex(index)
        for x in self.model().items_at_depth(item.depth):
            self.expand(self.model().indexFromItem(x))

    def show_context_menu(self, pos):
        index = self.indexAt(pos)
        m = QMenu(self)
        if index.isValid():
            m.addAction(QIcon.ic('plus.png'), _('Expand all items under %s') % index.data(), partial(self.expand_tree, index))
        m.addSeparator()
        m.addAction(QIcon.ic('plus.png'), _('Expand all items'), self.expandAll)
        m.addAction(QIcon.ic('minus.png'), _('Collapse all items'), self.collapseAll)
        m.addSeparator()
        if index.isValid():
            m.addAction(QIcon.ic('plus.png'), _('Expand all items at the level of {}').format(index.data()), partial(self.expand_at_level, index))
            m.addAction(QIcon.ic('minus.png'), _('Collapse all items at the level of {}').format(index.data()), partial(self.collapse_at_level, index))
        m.addSeparator()
        m.addAction(QIcon.ic('edit-copy.png'), _('Copy Table of Contents to clipboard'), self.copy_to_clipboard)
        self.context_menu = m
        m.exec(self.mapToGlobal(pos))

    def copy_to_clipboard(self):
        m = self.model()
        QApplication.clipboard().setText(getattr(m, 'as_plain_text', ''))

    def update_current_toc_nodes(self, families):
        self.model().update_current_toc_nodes(families)

    def scroll_to_current_toc_node(self):
        try:
            nodes = self.model().viewed_nodes()
        except AttributeError:
            nodes = ()
        if nodes:
            self.scrollTo(nodes[-1].index())


class TOCSearch(QWidget):

    def __init__(self, toc_view, parent=None):
        QWidget.__init__(self, parent)
        self.toc_view = toc_view
        self.l = l = QHBoxLayout(self)
        self.search = s = SearchBox2(self)
        self.search.setMinimumContentsLength(15)
        self.search.initialize('viewer_toc_search_history', help_text=_('Search Table of Contents'))
        self.search.setToolTip(_('Search for text in the Table of Contents'))
        s.search.connect(self.do_search)
        self.go = b = QToolButton(self)
        b.setIcon(QIcon.ic('search.png'))
        b.clicked.connect(s.do_search)
        b.setToolTip(_('Find next match'))
        l.addWidget(s), l.addWidget(b)

    def do_search(self, text):
        if not text or not text.strip():
            return
        delta = -1 if QApplication.instance().keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        index = self.toc_view.model().search(text, delta=delta)
        if index.isValid():
            self.toc_view.scrollTo(index)
            self.toc_view.searched.emit(index)
        else:
            error_dialog(self.toc_view, _('No matches found'), _(
                'There are no Table of Contents entries matching: %s') % text, show=True)
        self.search.search_done(True)


class TOCItem(QStandardItem):

    def __init__(self, toc, depth, all_items, normal_font, emphasis_font, depths, parent=None):
        text = toc.get('title') or ''
        if text == '--pipe-worker':
            text = _('Untitled')
        self.href = (toc.get('dest') or '')
        if toc.get('frag'):
            self.href += '#' + toc['frag']
        if text:
            text = re.sub(r'\s', ' ', text)
        self.title = text
        self.parent = parent
        self.node_id = toc['id']
        QStandardItem.__init__(self, text)
        all_items.append(self)
        self.normal_font, self.emphasis_font = normal_font, emphasis_font
        if toc['children']:
            depths.add(depth + 1)
            for t in toc['children']:
                self.appendRow(TOCItem(t, depth+1, all_items, normal_font, emphasis_font, depths, parent=self))
        self.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.is_current_search_result = False
        self.depth = depth
        self.set_being_viewed(False)

    def set_being_viewed(self, is_being_viewed):
        self.is_being_viewed = is_being_viewed
        self.setFont(self.emphasis_font if is_being_viewed else self.normal_font)

    @property
    def ancestors(self):
        parent = self.parent
        while parent is not None:
            yield parent
            parent = parent.parent

    @classmethod
    def type(cls):
        return QStandardItem.ItemType.UserType+10

    def set_current_search_result(self, yes):
        if yes and not self.is_current_search_result:
            self.setText(self.text() + ' ◄')
            self.is_current_search_result = True
        elif not yes and self.is_current_search_result:
            self.setText(self.text()[:-2])
            self.is_current_search_result = False

    def __repr__(self):
        indent = ' ' * self.depth
        return f'{indent}▶ TOC Item: {self.title} ({self.node_id})'

    def __str__(self):
        return repr(self)


class TOC(QStandardItemModel):

    current_toc_nodes_changed = pyqtSignal(object, object)

    def __init__(self, toc=None, additional_debug=False):
        QStandardItemModel.__init__(self)
        self.additional_debug = additional_debug
        self.current_query = {'text':'', 'index':-1, 'items':()}
        self.all_items = depth_first = []
        normal_font = QApplication.instance().font()
        emphasis_font = QFont(normal_font)
        emphasis_font.setBold(True), emphasis_font.setItalic(True)
        self.depths = {0}
        if toc:
            for t in toc['children']:
                self.appendRow(TOCItem(t, 0, depth_first, normal_font, emphasis_font, self.depths))
        self.depths = tuple(sorted(self.depths))
        self.node_id_map = {x.node_id: x for x in self.all_items}
        self.current_toc_leaf_ids = ()
        self.current_toc_generation = 0

    def find_items(self, query):
        for item in self.all_items:
            text = item.text()
            if query and isinstance(query, str):
                if text and isinstance(text, str) and primary_contains(query, text):
                    yield item
            else:
                yield item

    def items_at_depth(self, depth):
        for item in self.all_items:
            if item.depth == depth:
                yield item

    def node_id_for_text(self, query):
        for item in self.find_items(query):
            return item.node_id

    def node_id_for_href(self, query, exact=False):
        for item in self.all_items:
            href = item.href
            if (exact and query == href) or (not exact and query in href):
                return item.node_id

    def search(self, query, delta=1):
        cq = self.current_query
        if cq['items'] and -1 < cq['index'] < len(cq['items']):
            cq['items'][cq['index']].set_current_search_result(False)
        if cq['text'] != query:
            items = tuple(self.find_items(query))
            cq.update({'text':query, 'items':items, 'index':-1})
        num = len(cq['items'])
        if num > 0:
            cq['index'] = (cq['index'] + delta + num) % num
            item = cq['items'][cq['index']]
            item.set_current_search_result(True)
            index = self.indexFromItem(item)
            return index
        return QModelIndex()

    def update_current_toc_nodes(self, current_toc_leaves):
        current_toc_leaves = tuple(current_toc_leaves)
        self.current_toc_leaf_ids = current_toc_leaves
        self.current_toc_generation += 1
        viewed_nodes = set()
        ancestors = {}
        missing = [] if self.additional_debug else None
        for node_id in current_toc_leaves:
            node = self.node_id_map.get(node_id)
            if node is not None:
                viewed_nodes.add(node_id)
                ansc = tuple(node.ancestors)
                viewed_nodes |= {x.node_id for x in ansc}
                for x in ansc:
                    ancestors[x.node_id] = x.index()
            elif missing is not None:
                missing.append(node_id)
        nodes = []
        changed_nodes = []
        for node in self.all_items:
            is_being_viewed = node.node_id in viewed_nodes
            if is_being_viewed:
                nodes.append(node)
            if is_being_viewed != node.is_being_viewed:
                node.set_being_viewed(is_being_viewed)
                changed_nodes.append(node)
        if self.additional_debug:
            prints(
                'TOC-HILITE-DIAG qt.update-current-toc-nodes',
                {
                    'input_leaf_ids': tuple(current_toc_leaves), 'missing_leaf_ids': tuple(missing),
                    'viewed_node_ids': tuple(node.node_id for node in nodes),
                    'viewed_titles': tuple(node.title for node in nodes),
                    'changed_node_ids': tuple(node.node_id for node in changed_nodes),
                },
                file=sys.stderr,
            )
        self.current_toc_nodes_changed.emit(tuple(ancestors.values()), nodes)

    def viewed_nodes(self):
        return tuple(node for node in self.all_items if node.is_being_viewed)

    @property
    def title_for_current_node(self):
        for node in reversed(self.all_items):
            if node.is_being_viewed:
                return node.title

    @property
    def as_plain_text(self):
        lines = []
        for item in self.all_items:
            lines.append(' ' * (4 * item.depth) + (item.title or ''))
        return '\n'.join(lines)
