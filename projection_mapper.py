from __future__ import annotations

import json
import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QGuiApplication,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


SCENE_W = 1280
SCENE_H = 720
HANDLE_SIZE_INPUT = 10.0
HANDLE_SIZE_OUTPUT = 6.0
MIN_SHAPE_SIZE = 8.0
OUTPUT_HANDLE_SHOW_DISTANCE = 18.0
MAX_HISTORY = 120
DEFAULT_STROKE_WIDTH = 2.0

GLOBAL_COLOR_PHASE = 0.0
GLOBAL_PATTERN_PHASE = 0.0


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def color_to_hex(color: QColor) -> str:
    return color.name(QColor.NameFormat.HexArgb)


def color_from_hex(text: str) -> QColor:
    c = QColor(text)
    return c if c.isValid() else QColor("white")


class EditableShapeItem(QGraphicsObject):
    changed = Signal()
    editStarted = Signal()
    editFinished = Signal()

    EDGE_INSERT_DISTANCE = 10.0

    def __init__(
        self,
        kind: str,
        color: QColor,
        points: Optional[List[QPointF]] = None,
        rect: Optional[QRectF] = None,
    ):
        super().__init__()
        self.kind = kind
        self.fill_color = QColor(color)
        self.stroke_color = QColor("white")
        self.stroke_visible = True
        self.stroke_width = DEFAULT_STROKE_WIDTH
        self.animate_color = False
        self.animate_pattern = False
        self.selected_handle: Optional[int] = None
        self.dragging_handle = False
        self._editing_started = False
        self._drag_started = False
        self._points: List[QPointF] = [QPointF(p) for p in (points or [])]
        self._rect = QRectF(rect) if rect else QRectF(0, 0, 0, 0)

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, True)
        self.setAcceptHoverEvents(True)

    def itemChange(self, change, value):
        if change in (
            QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged,
            QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged,
        ):
            self.changed.emit()
        return super().itemChange(change, value)

    def boundingRect(self) -> QRectF:
        rect = self.local_path().boundingRect()
        pad = HANDLE_SIZE_INPUT + 8
        return rect.adjusted(-pad, -pad, pad, pad)

    def local_path(self) -> QPainterPath:
        path = QPainterPath()
        if self.kind == "polygon":
            if len(self._points) >= 2:
                path.addPolygon(QPolygonF(self._points))
                path.closeSubpath()
        elif self.kind == "rect":
            path.addRect(self._rect)
        elif self.kind == "ellipse":
            path.addEllipse(self._rect)
        return path

    def scene_path(self) -> QPainterPath:
        return self.sceneTransform().map(self.local_path())

    def effective_fill_color(self) -> QColor:
        base = QColor(self.fill_color)
        if self.animate_color:
            h, s, v, a = base.getHsv()
            if h < 0:
                h = 0
            base = QColor.fromHsv(int((h + GLOBAL_COLOR_PHASE) % 360), max(160, s), v, a)
        return base

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = self.local_path()
        fill = self.effective_fill_color()
        pen = QPen(self.stroke_color if self.stroke_visible else Qt.GlobalColor.transparent, self.stroke_width)
        painter.setPen(pen)
        painter.setBrush(fill)
        painter.drawPath(path)

        if self.animate_pattern:
            painter.save()
            painter.setClipPath(path)
            stripe_spacing = 26
            offset = GLOBAL_PATTERN_PHASE % stripe_spacing
            painter.setPen(QPen(QColor(255, 255, 255, 95), 6))
            bounds = self.boundingRect().adjusted(-60, -60, 60, 60)
            x_start = int(bounds.left() - bounds.height())
            x_end = int(bounds.right() + bounds.height())
            for x in range(x_start, x_end, stripe_spacing):
                painter.drawLine(
                    QPointF(x + offset, bounds.top()),
                    QPointF(x + offset + bounds.height(), bounds.bottom()),
                )
            painter.restore()

        if self.isSelected():
            painter.setBrush(QColor("#00d4ff"))
            painter.setPen(QPen(Qt.GlobalColor.black, 1))
            for r in self.handle_rects(HANDLE_SIZE_INPUT):
                painter.drawRect(r)

            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#00d4ff"), 1, Qt.PenStyle.DashLine))
            painter.drawPath(path)

    def hoverMoveEvent(self, event):
        idx = self.handle_at(event.pos(), HANDLE_SIZE_INPUT)
        if idx is not None:
            QApplication.setOverrideCursor(Qt.CursorShape.SizeAllCursor)
        else:
            QApplication.restoreOverrideCursor()
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        QApplication.restoreOverrideCursor()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        self._editing_started = False
        self._drag_started = False
        idx = self.handle_at(event.pos(), HANDLE_SIZE_INPUT)
        if idx is not None:
            self.selected_handle = idx
            self.dragging_handle = True
            self._editing_started = True
            self.editStarted.emit()
            event.accept()
            return

        if self.kind == "polygon" and self.isSelected():
            seg_idx, projected = self.segment_near(event.pos(), self.EDGE_INSERT_DISTANCE)
            if seg_idx is not None and projected is not None:
                self.prepareGeometryChange()
                self.editStarted.emit()
                self._points.insert(seg_idx + 1, projected)
                self.selected_handle = seg_idx + 1
                self.dragging_handle = True
                self._editing_started = True
                self.update()
                self.changed.emit()
                event.accept()
                return

        self.dragging_handle = False
        self.selected_handle = None
        self._drag_started = True
        self.editStarted.emit()
        super().mousePressEvent(event)
        self.changed.emit()

    def mouseMoveEvent(self, event):
        if self.dragging_handle and self.selected_handle is not None:
            self.prepareGeometryChange()
            self.update_handle(self.selected_handle, event.pos())
            self.update()
            self.changed.emit()
            event.accept()
            return
        super().mouseMoveEvent(event)
        self.changed.emit()

    def mouseReleaseEvent(self, event):
        had_edit = self.dragging_handle or self._drag_started
        self.dragging_handle = False
        self.selected_handle = None
        self.changed.emit()
        super().mouseReleaseEvent(event)
        if had_edit:
            self.editFinished.emit()

    def handle_rects(self, size: float) -> List[QRectF]:
        hs = size / 2
        handles: List[QRectF] = []
        if self.kind == "polygon":
            for pt in self._points:
                handles.append(QRectF(pt.x() - hs, pt.y() - hs, size, size))
        else:
            r = self._rect.normalized()
            for c in [r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft()]:
                handles.append(QRectF(c.x() - hs, c.y() - hs, size, size))
        return handles

    def handle_points(self) -> List[QPointF]:
        if self.kind == "polygon":
            return [QPointF(p) for p in self._points]
        r = self._rect.normalized()
        return [r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft()]

    def handle_at(self, pos: QPointF, size: float) -> Optional[int]:
        for i, r in enumerate(self.handle_rects(size)):
            if r.contains(pos):
                return i
        return None

    def _distance_point_to_segment(self, p: QPointF, a: QPointF, b: QPointF) -> Tuple[float, QPointF]:
        ax, ay = a.x(), a.y()
        bx, by = b.x(), b.y()
        px, py = p.x(), p.y()
        abx = bx - ax
        aby = by - ay
        ab_len_sq = abx * abx + aby * aby
        if ab_len_sq <= 1e-9:
            projected = QPointF(ax, ay)
            return math.hypot(px - ax, py - ay), projected
        t = ((px - ax) * abx + (py - ay) * aby) / ab_len_sq
        t = clamp(t, 0.0, 1.0)
        projected = QPointF(ax + t * abx, ay + t * aby)
        return math.hypot(px - projected.x(), py - projected.y()), projected

    def segment_near(self, pos: QPointF, threshold: float) -> Tuple[Optional[int], Optional[QPointF]]:
        if self.kind != "polygon" or len(self._points) < 2:
            return None, None
        best_idx: Optional[int] = None
        best_projected: Optional[QPointF] = None
        best_dist = float("inf")
        for i in range(len(self._points)):
            a = self._points[i]
            b = self._points[(i + 1) % len(self._points)]
            dist, projected = self._distance_point_to_segment(pos, a, b)
            if dist < best_dist:
                best_dist = dist
                best_idx = i
                best_projected = projected
        if best_dist <= threshold:
            return best_idx, best_projected
        return None, None

    def update_handle(self, idx: int, pos: QPointF):
        pos = self._clamp_point(pos)
        if self.kind == "polygon":
            self._points[idx] = pos
            return

        r = QRectF(self._rect)
        if idx == 0:
            r.setTopLeft(pos)
        elif idx == 1:
            r.setTopRight(pos)
        elif idx == 2:
            r.setBottomRight(pos)
        elif idx == 3:
            r.setBottomLeft(pos)
        r = r.normalized()
        if r.width() < MIN_SHAPE_SIZE:
            r.setWidth(MIN_SHAPE_SIZE)
        if r.height() < MIN_SHAPE_SIZE:
            r.setHeight(MIN_SHAPE_SIZE)
        self._rect = r

    def _clamp_point(self, pt: QPointF) -> QPointF:
        return QPointF(clamp(pt.x(), 0.0, SCENE_W), clamp(pt.y(), 0.0, SCENE_H))

    def set_fill_color(self, color: QColor):
        self.fill_color = QColor(color)
        self.update()
        self.changed.emit()

    def set_stroke_color(self, color: QColor):
        self.stroke_color = QColor(color)
        self.update()
        self.changed.emit()

    def toggle_stroke_visible(self):
        self.stroke_visible = not self.stroke_visible
        self.update()
        self.changed.emit()

    def set_random_fill_color(self):
        self.set_fill_color(QColor.fromHsv(random.randint(0, 359), 230, 255))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "fill_color": color_to_hex(self.fill_color),
            "stroke_color": color_to_hex(self.stroke_color),
            "stroke_visible": self.stroke_visible,
            "stroke_width": self.stroke_width,
            "animate_color": self.animate_color,
            "animate_pattern": self.animate_pattern,
            "pos": [self.pos().x(), self.pos().y()],
            "points": [[p.x(), p.y()] for p in self._points],
            "rect": [self._rect.x(), self._rect.y(), self._rect.width(), self._rect.height()],
            "selected": self.isSelected(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EditableShapeItem":
        kind = data.get("kind", "polygon")
        item = cls(
            kind,
            color_from_hex(data.get("fill_color", "#ffff006e")),
            points=[QPointF(float(x), float(y)) for x, y in data.get("points", [])],
            rect=QRectF(*data.get("rect", [0, 0, 0, 0])),
        )
        item.stroke_color = color_from_hex(data.get("stroke_color", "#ffffffff"))
        item.stroke_visible = bool(data.get("stroke_visible", True))
        item.stroke_width = float(data.get("stroke_width", DEFAULT_STROKE_WIDTH))
        item.animate_color = bool(data.get("animate_color", False))
        item.animate_pattern = bool(data.get("animate_pattern", False))
        pos = data.get("pos", [0, 0])
        item.setPos(QPointF(float(pos[0]), float(pos[1])))
        item.setSelected(bool(data.get("selected", False)))
        return item


class CanvasView(QGraphicsView):
    cursorMovedInScene = Signal(QPointF)
    contentChanged = Signal()
    historyCommitRequested = Signal(str)

    def __init__(self, scene: QGraphicsScene):
        super().__init__(scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.Shape.Box)
        self.setBackgroundBrush(QColor("#111111"))
        self.setSceneRect(0, 0, SCENE_W, SCENE_H)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)

        self.current_tool = "select"
        self.current_color = QColor("#ff006e")
        self.temp_points: List[QPointF] = []
        self.temp_rect_start: Optional[QPointF] = None
        self.temp_rect_current: Optional[QPointF] = None
        self.last_scene_mouse_pos = QPointF(SCENE_W / 2, SCENE_H / 2)
        self.temp_polygon_undo_stack: List[List[QPointF]] = []
        self.temp_polygon_redo_stack: List[List[QPointF]] = []
        self.zoom_factor = 1.0
        self.pan_offset = QPointF(0, 0)
        self._is_panning = False
        self._last_pan_view_pos = QPoint()
        self.mouse_pan_speed = 1.0
        self.gesture_pan_speed = 0.6

    def set_tool(self, tool: str):
        self.current_tool = tool
        self.cancel_temp_shape()
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag if tool == "select" else QGraphicsView.DragMode.NoDrag)
        self.viewport().update()
        self.contentChanged.emit()

    def cancel_temp_shape(self):
        self.temp_points.clear()
        self.temp_rect_start = None
        self.temp_rect_current = None
        self.temp_polygon_undo_stack.clear()
        self.temp_polygon_redo_stack.clear()
        self.viewport().update()
        self.contentChanged.emit()

    def _clone_points(self, points: List[QPointF]) -> List[QPointF]:
        return [QPointF(p) for p in points]

    def _push_temp_polygon_history(self):
        self.temp_polygon_undo_stack.append(self._clone_points(self.temp_points))
        if len(self.temp_polygon_undo_stack) > MAX_HISTORY:
            self.temp_polygon_undo_stack = self.temp_polygon_undo_stack[-MAX_HISTORY:]
        self.temp_polygon_redo_stack.clear()

    def _refresh_polygon_preview_tail(self):
        if self.current_tool == "polygon" and self.temp_points:
            self.temp_points[-1] = QPointF(self.last_scene_mouse_pos)

    def undo_temp_polygon(self) -> bool:
        if self.current_tool != "polygon" or not self.temp_polygon_undo_stack:
            return False
        self.temp_polygon_redo_stack.append(self._clone_points(self.temp_points))
        self.temp_points = self.temp_polygon_undo_stack.pop()
        self._refresh_polygon_preview_tail()
        self.viewport().update()
        self.contentChanged.emit()
        return True

    def redo_temp_polygon(self) -> bool:
        if self.current_tool != "polygon" or not self.temp_polygon_redo_stack:
            return False
        self.temp_polygon_undo_stack.append(self._clone_points(self.temp_points))
        self.temp_points = self.temp_polygon_redo_stack.pop()
        self._refresh_polygon_preview_tail()
        self.viewport().update()
        self.contentChanged.emit()
        return True

    def drawForeground(self, painter: QPainter, rect: QRectF):
        super().drawForeground(painter, rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        painter.setPen(QPen(QColor(50, 50, 50), 1))
        step = 40
        left = int(rect.left()) - (int(rect.left()) % step)
        top = int(rect.top()) - (int(rect.top()) % step)
        for x in range(left, int(rect.right()) + step, step):
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        for y in range(top, int(rect.bottom()) + step, step):
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))

        painter.setPen(QPen(QColor("#00d4ff"), 2, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.current_tool == "polygon" and len(self.temp_points) >= 2:
            painter.drawPolyline(QPolygonF(self.temp_points))
        elif self.current_tool in ("rect", "ellipse") and self.temp_rect_start and self.temp_rect_current:
            r = QRectF(self.temp_rect_start, self.temp_rect_current).normalized()
            if self.current_tool == "rect":
                painter.drawRect(r)
            else:
                painter.drawEllipse(r)

    def mouseMoveEvent(self, event):
        if self._is_panning:
            delta = event.pos() - self._last_pan_view_pos
            self._last_pan_view_pos = QPoint(event.pos())
            self._pan_by_view_delta(delta.x(), delta.y(), speed=self.mouse_pan_speed, invert=True)
            event.accept()
            return

        scene_pos = self.mapToScene(event.pos())
        self.last_scene_mouse_pos = QPointF(scene_pos)
        self.cursorMovedInScene.emit(scene_pos)
        if self.current_tool == "polygon" and self.temp_points:
            self.temp_points[-1] = scene_pos
            self.viewport().update()
            self.contentChanged.emit()
        elif self.current_tool in ("rect", "ellipse") and self.temp_rect_start is not None:
            self.temp_rect_current = scene_pos
            self.viewport().update()
            self.contentChanged.emit()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.cursorMovedInScene.emit(QPointF(-1, -1))
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        scene_pos = self.mapToScene(event.pos())

        if event.button() == Qt.MouseButton.MiddleButton:
            self._is_panning = True
            self._last_pan_view_pos = QPoint(event.pos())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton:
            self._is_panning = True
            self._last_pan_view_pos = QPoint(event.pos())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            if self.current_tool == "polygon":
                self._push_temp_polygon_history()
                if not self.temp_points:
                    self.temp_points = [scene_pos, scene_pos]
                else:
                    self.temp_points[-1] = scene_pos
                    self.temp_points.append(scene_pos)
                self.viewport().update()
                self.contentChanged.emit()
                event.accept()
                return
            if self.current_tool in ("rect", "ellipse"):
                self.temp_rect_start = scene_pos
                self.temp_rect_current = scene_pos
                self.viewport().update()
                self.contentChanged.emit()
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.current_tool == "polygon" and len(self.temp_points) >= 4:
            final_points = self.temp_points[:-1]
            item = EditableShapeItem("polygon", self.current_color, points=final_points)
            self.scene().addItem(item)
            self._wire_item(item)
            self.scene().clearSelection()
            item.setSelected(True)
            self.cancel_temp_shape()
            self.contentChanged.emit()
            self.historyCommitRequested.emit("Add polygon")
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton) and self._is_panning:
            self._is_panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton and self.current_tool in ("rect", "ellipse"):
            if self.temp_rect_start and self.temp_rect_current:
                r = QRectF(self.temp_rect_start, self.temp_rect_current).normalized()
                if r.width() >= MIN_SHAPE_SIZE and r.height() >= MIN_SHAPE_SIZE:
                    item = EditableShapeItem(self.current_tool, self.current_color, rect=r)
                    self.scene().addItem(item)
                    self._wire_item(item)
                    self.scene().clearSelection()
                    item.setSelected(True)
                    self.contentChanged.emit()
                    self.historyCommitRequested.emit(f"Add {self.current_tool}")
            self.cancel_temp_shape()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QWheelEvent):
        pixel_delta = event.pixelDelta()
        angle_delta = event.angleDelta()
        wants_zoom = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        source = event.source()

        # On Windows touchpads, two-finger drag/scroll often comes through as a synthesized
        # wheel event. Treat those as panning unless Ctrl is held explicitly for zoom.
        is_touchpad_like = (
            not pixel_delta.isNull()
            or source != Qt.MouseEventSource.MouseEventNotSynthesized
        )

        if not wants_zoom and is_touchpad_like:
            dx = pixel_delta.x() if not pixel_delta.isNull() else angle_delta.x() / 4.0
            dy = pixel_delta.y() if not pixel_delta.isNull() else angle_delta.y() / 4.0
            if dx or dy:
                self._pan_by_view_delta(dx, dy, speed=self.gesture_pan_speed, invert=True)
                event.accept()
                return

        # Keep regular mouse-wheel zoom, and also allow Ctrl + touchpad gesture zoom.
        delta = angle_delta.y()
        if delta == 0 and not pixel_delta.isNull():
            delta = pixel_delta.y()
        if delta:
            self._apply_zoom_delta(delta)
            event.accept()
            return
        super().wheelEvent(event)

    def _apply_zoom_delta(self, delta: float):
        factor = 1.12 if delta > 0 else 1 / 1.12
        new_zoom = clamp(self.zoom_factor * factor, 0.15, 20.0)
        factor = new_zoom / self.zoom_factor
        self.zoom_factor = new_zoom
        self.scale(factor, factor)
        center = self.mapToScene(self.viewport().rect().center())
        self.pan_offset = center
        self.contentChanged.emit()

    def _pan_by_view_delta(self, dx: float, dy: float, speed: float = 1.0, invert: bool = False):
        if invert:
            dx = -dx
            dy = -dy

        current_center_view = self.viewport().rect().center()
        current_center_scene = self.mapToScene(current_center_view)

        scene_before = self.mapToScene(QPoint(int(current_center_view.x()), int(current_center_view.y())))
        scene_after = self.mapToScene(QPoint(int(current_center_view.x() + dx), int(current_center_view.y() + dy)))
        scene_delta = scene_after - scene_before

        new_center = current_center_scene + (scene_delta * speed)
        self.centerOn(new_center)
        self.pan_offset = new_center
        self.contentChanged.emit()

    def reset_zoom(self):
        self.resetTransform()
        self.zoom_factor = 1.0
        self.centerOn(SCENE_W / 2, SCENE_H / 2)
        self.pan_offset = QPointF(SCENE_W / 2, SCENE_H / 2)
        self.contentChanged.emit()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            for item in list(self.scene().selectedItems()):
                self.scene().removeItem(item)
            self.contentChanged.emit()
            self.historyCommitRequested.emit("Delete selected")
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.cancel_temp_shape()
            event.accept()
            return
        super().keyPressEvent(event)

    def _wire_item(self, item: EditableShapeItem):
        item.changed.connect(self.contentChanged.emit)
        item.editStarted.connect(lambda: self.historyCommitRequested.emit("Pre-edit"))
        item.editFinished.connect(lambda: self.historyCommitRequested.emit("Edit shape"))

    def temp_preview_path(self) -> Optional[QPainterPath]:
        path = QPainterPath()
        if self.current_tool == "polygon" and len(self.temp_points) >= 2:
            path.addPolygon(QPolygonF(self.temp_points))
            return path
        if self.current_tool in ("rect", "ellipse") and self.temp_rect_start and self.temp_rect_current:
            r = QRectF(self.temp_rect_start, self.temp_rect_current).normalized()
            if self.current_tool == "rect":
                path.addRect(r)
            else:
                path.addEllipse(r)
            return path
        return None


class OutputWindow(QWidget):
    def __init__(self, scene: QGraphicsScene, canvas: CanvasView):
        super().__init__()
        self.scene = scene
        self.canvas = canvas
        self.cursor_scene_pos = QPointF(-1, -1)
        self.current_screen_index = 0
        self.setWindowTitle("Projection Output")
        self.setStyleSheet("background-color: black;")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.resize(960, 540)

    def set_cursor_scene_pos(self, pos: QPointF):
        self.cursor_scene_pos = pos
        self.update()

    def available_screens(self):
        return QGuiApplication.screens()

    def apply_to_screen(self, screen_index: int, fullscreen: bool = True):
        screens = self.available_screens()
        if not screens:
            return
        screen_index = max(0, min(screen_index, len(screens) - 1))
        self.current_screen_index = screen_index
        screen = screens[screen_index]
        if self.windowHandle() is not None:
            self.windowHandle().setScreen(screen)
        geom = screen.geometry()
        self.showNormal()
        self.setGeometry(geom)
        if fullscreen:
            self.showFullScreen()
        else:
            self.show()
        self.raise_()
        self.update()

    def move_to_next_screen(self):
        screens = self.available_screens()
        if not screens:
            return
        self.apply_to_screen((self.current_screen_index + 1) % len(screens), fullscreen=self.isFullScreen())

    def set_borderless_windowed(self):
        screens = self.available_screens()
        if not screens:
            return
        screen = screens[self.current_screen_index]
        geom = screen.availableGeometry()
        self.showNormal()
        self.setGeometry(geom)
        self.show()
        self.raise_()
        self.update()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.set_borderless_windowed()
        else:
            self.apply_to_screen(self.current_screen_index, fullscreen=True)

    def map_scene_to_output(self, pt: QPointF) -> QPointF:
        return QPointF(pt.x() * self.width() / SCENE_W, pt.y() * self.height() / SCENE_H)

    def scaled_path(self, path: QPainterPath) -> QPainterPath:
        t = QTransform()
        t.scale(self.width() / SCENE_W, self.height() / SCENE_H)
        return t.map(path)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)

        for item in [i for i in self.scene.items() if isinstance(i, EditableShapeItem)]:
            path = self.scaled_path(item.scene_path())
            fill = item.effective_fill_color()
            pen = QPen(item.stroke_color if item.stroke_visible else Qt.GlobalColor.transparent, item.stroke_width)
            painter.setPen(pen)
            painter.setBrush(fill)
            painter.drawPath(path)

            if item.animate_pattern:
                painter.save()
                painter.setClipPath(path)
                stripe_spacing = 38
                offset = GLOBAL_PATTERN_PHASE % stripe_spacing
                painter.setPen(QPen(QColor(255, 255, 255, 70), 10))
                for x in range(-self.height(), self.width() + self.height(), stripe_spacing):
                    painter.drawLine(x + offset, 0, x + offset + self.height(), self.height())
                painter.restore()

        temp_path = self.canvas.temp_preview_path()
        if temp_path is not None:
            preview = self.scaled_path(temp_path)
            painter.setPen(QPen(QColor("#00d4ff"), 2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(preview)

        # Small output edit points only appear when mouse is near them.
        if self.canvas.current_tool == "select":
            cursor_valid = 0 <= self.cursor_scene_pos.x() <= SCENE_W and 0 <= self.cursor_scene_pos.y() <= SCENE_H
            if cursor_valid:
                for item in [i for i in self.scene.selectedItems() if isinstance(i, EditableShapeItem)]:
                    for pt in item.handle_points():
                        scene_pt = item.mapToScene(pt)
                        dist = math.hypot(scene_pt.x() - self.cursor_scene_pos.x(), scene_pt.y() - self.cursor_scene_pos.y())
                        if dist <= OUTPUT_HANDLE_SHOW_DISTANCE:
                            out_pt = self.map_scene_to_output(scene_pt)
                            painter.setBrush(QColor("#00d4ff"))
                            painter.setPen(QPen(Qt.GlobalColor.black, 1))
                            r = HANDLE_SIZE_OUTPUT
                            painter.drawEllipse(QPointF(out_pt.x(), out_pt.y()), r, r)

        if 0 <= self.cursor_scene_pos.x() <= SCENE_W and 0 <= self.cursor_scene_pos.y() <= SCENE_H:
            out_pt = self.map_scene_to_output(self.cursor_scene_pos)
            painter.setPen(QPen(QColor("#00d4ff"), 1))
            painter.drawLine(int(out_pt.x()), 0, int(out_pt.x()), self.height())
            painter.drawLine(0, int(out_pt.y()), self.width(), int(out_pt.y()))

        painter.end()


@dataclass
class HistoryState:
    label: str
    payload: str


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Projection Mapping Sketch Tool")
        self.resize(1660, 940)

        self.scene = QGraphicsScene(0, 0, SCENE_W, SCENE_H)
        self.canvas = CanvasView(self.scene)
        self.output_window = OutputWindow(self.scene, self.canvas)

        self.undo_stack: List[HistoryState] = []
        self.redo_stack: List[HistoryState] = []
        self.restoring_history = False
        self.last_saved_path: Optional[Path] = None

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        info = QLabel(
            "Input / drawing window\n"
            "- Right-click drag pans the input view\n"
            "- Mouse wheel or touchpad zoom only affects the input view\n"
            "- Double-click finishes freehand polygon\n"
            "- Ctrl+Z undo, Ctrl+Y redo, Ctrl+S save, Ctrl+O load"
        )
        info.setStyleSheet("padding: 8px; background:#202020; color:white;")
        left_layout.addWidget(info)
        left_layout.addWidget(self.canvas, stretch=1)

        side_outer = QWidget()
        side_outer_layout = QVBoxLayout(side_outer)
        side_outer_layout.setContentsMargins(0, 0, 0, 0)
        side_outer_layout.setSpacing(8)
        side_outer.setFixedWidth(300)
        side_outer.setStyleSheet("background:#151515; color:white;")

        self.status_label = QLabel("Tool: Select / Edit")
        self.status_label.setStyleSheet("padding: 6px; background:#202020;")
        side_outer_layout.addWidget(self.status_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        tools = QWidget()
        tools_layout = QVBoxLayout(tools)
        tools_layout.setContentsMargins(8, 8, 8, 8)
        tools_layout.setSpacing(8)
        scroll.setWidget(tools)

        self.screen_combo = QComboBox()
        self.screen_combo.setMinimumHeight(36)
        self.refresh_screens()
        self.screen_combo.currentIndexChanged.connect(self.on_screen_selected)

        btn_select = QPushButton("Select / Edit")
        btn_poly = QPushButton("Freehand Polygon")
        btn_rect = QPushButton("Rectangle")
        btn_ellipse = QPushButton("Circle / Ellipse")
        btn_fill = QPushButton("Set Fill Color")
        btn_random = QPushButton("Randomize Selected Colors")
        btn_cycle = QPushButton("Toggle Color Cycle")
        btn_pattern = QPushButton("Toggle Moving Pattern")
        btn_toggle_border = QPushButton("Toggle Border On / Off")
        btn_border_color = QPushButton("Set Border Color")
        btn_reset_zoom = QPushButton("Reset Input Zoom")
        btn_save = QPushButton("Save Project")
        btn_load = QPushButton("Load Project")
        btn_clear = QPushButton("Clear All")
        btn_output_apply = QPushButton("Send Output To Selected Screen")
        btn_output_next = QPushButton("Move Output To Next Screen")
        btn_output_fs = QPushButton("Output Fullscreen / Window")

        all_buttons = [
            btn_select, btn_poly, btn_rect, btn_ellipse,
            btn_fill, btn_random, btn_cycle, btn_pattern,
            btn_toggle_border, btn_border_color, btn_reset_zoom,
            btn_save, btn_load, btn_clear,
            btn_output_apply, btn_output_next, btn_output_fs,
        ]
        for btn in all_buttons:
            btn.setMinimumHeight(40)

        btn_select.clicked.connect(lambda: self.set_tool("select", "Select / Edit"))
        btn_poly.clicked.connect(lambda: self.set_tool("polygon", "Freehand Polygon"))
        btn_rect.clicked.connect(lambda: self.set_tool("rect", "Rectangle"))
        btn_ellipse.clicked.connect(lambda: self.set_tool("ellipse", "Circle / Ellipse"))
        btn_fill.clicked.connect(self.set_selected_fill_color)
        btn_random.clicked.connect(self.randomize_selected_colors)
        btn_cycle.clicked.connect(self.toggle_color_cycle)
        btn_pattern.clicked.connect(self.toggle_pattern_animation)
        btn_toggle_border.clicked.connect(self.toggle_selected_borders)
        btn_border_color.clicked.connect(self.set_selected_border_color)
        btn_reset_zoom.clicked.connect(self.canvas.reset_zoom)
        btn_save.clicked.connect(self.save_project_dialog)
        btn_load.clicked.connect(self.load_project_dialog)
        btn_clear.clicked.connect(self.clear_all)
        btn_output_apply.clicked.connect(self.apply_selected_output_screen)
        btn_output_next.clicked.connect(self.move_output_to_next_screen)
        btn_output_fs.clicked.connect(self.output_window.toggle_fullscreen)

        tools_layout.addWidget(btn_select)
        tools_layout.addWidget(btn_poly)
        tools_layout.addWidget(btn_rect)
        tools_layout.addWidget(btn_ellipse)
        tools_layout.addWidget(btn_fill)
        tools_layout.addWidget(btn_random)
        tools_layout.addWidget(btn_cycle)
        tools_layout.addWidget(btn_pattern)
        tools_layout.addWidget(btn_toggle_border)
        tools_layout.addWidget(btn_border_color)
        tools_layout.addWidget(btn_reset_zoom)
        tools_layout.addWidget(btn_save)
        tools_layout.addWidget(btn_load)
        tools_layout.addWidget(btn_clear)
        tools_layout.addSpacing(12)
        tools_layout.addWidget(QLabel("Output monitor:"))
        tools_layout.addWidget(self.screen_combo)
        tools_layout.addWidget(btn_output_apply)
        tools_layout.addWidget(btn_output_next)
        tools_layout.addWidget(btn_output_fs)
        tools_layout.addStretch(1)

        side_outer_layout.addWidget(scroll, stretch=1)

        layout.addWidget(left_panel, stretch=1)
        layout.addWidget(side_outer)
        self.setCentralWidget(central)

        self.canvas.cursorMovedInScene.connect(self.output_window.set_cursor_scene_pos)
        self.canvas.contentChanged.connect(self.refresh_views)
        self.canvas.historyCommitRequested.connect(self.on_history_commit_request)
        self.scene.selectionChanged.connect(self.refresh_views)

        self.setup_shortcuts()

        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self.advance_animation)
        self.animation_timer.start(33)

        initial_index = 1 if len(QApplication.screens()) > 1 else 0
        self.screen_combo.setCurrentIndex(initial_index)
        self.output_window.apply_to_screen(initial_index, fullscreen=False)

        self.push_history("Initial")

    def refresh_views(self):
        self.scene.update()
        self.canvas.viewport().update()
        self.output_window.update()

    def refresh_screens(self):
        self.screen_combo.blockSignals(True)
        self.screen_combo.clear()
        for idx, screen in enumerate(QApplication.screens()):
            geom = screen.geometry()
            name = screen.name() or f"Display {idx + 1}"
            self.screen_combo.addItem(f"{idx + 1}: {name} ({geom.width()}x{geom.height()})", idx)
        self.screen_combo.blockSignals(False)

    def on_screen_selected(self, index: int):
        if index >= 0:
            self.output_window.current_screen_index = index

    def apply_selected_output_screen(self):
        index = self.screen_combo.currentIndex()
        if index >= 0:
            self.output_window.apply_to_screen(index, fullscreen=True)

    def move_output_to_next_screen(self):
        self.output_window.move_to_next_screen()
        self.screen_combo.blockSignals(True)
        self.screen_combo.setCurrentIndex(self.output_window.current_screen_index)
        self.screen_combo.blockSignals(False)

    def setup_shortcuts(self):
        def add_shortcut(name: str, seq: str | QKeySequence, slot):
            action = QAction(name, self)
            action.setShortcut(QKeySequence(seq) if isinstance(seq, str) else seq)
            action.triggered.connect(slot)
            self.addAction(action)

        add_shortcut("Delete", Qt.Key.Key_Delete, self.delete_selected)
        add_shortcut("Exit Output Fullscreen", Qt.Key.Key_Escape, self.output_escape_behavior)
        add_shortcut("Fullscreen Output", "F", self.output_window.toggle_fullscreen)
        add_shortcut("Next Output Screen", "N", self.move_output_to_next_screen)
        add_shortcut("Undo", QKeySequence.StandardKey.Undo, self.undo)
        add_shortcut("Redo", QKeySequence.StandardKey.Redo, self.redo)
        add_shortcut("Save", QKeySequence.StandardKey.Save, self.save_project_dialog)
        add_shortcut("Load", QKeySequence.StandardKey.Open, self.load_project_dialog)

    def output_escape_behavior(self):
        if self.output_window.isFullScreen():
            self.output_window.toggle_fullscreen()
        else:
            self.canvas.cancel_temp_shape()

    def set_tool(self, tool: str, label: str):
        self.canvas.set_tool(tool)
        self.status_label.setText(f"Tool: {label}")
        self.refresh_views()

    def selected_shape_items(self) -> List[EditableShapeItem]:
        return [i for i in self.scene.selectedItems() if isinstance(i, EditableShapeItem)]

    def _require_selection(self) -> List[EditableShapeItem]:
        items = self.selected_shape_items()
        if not items:
            QMessageBox.information(self, "No selection", "Select one or more shapes first.")
        return items

    def set_selected_fill_color(self):
        items = self._require_selection()
        if not items:
            return
        color = QColorDialog.getColor(items[0].fill_color, self, "Choose fill color")
        if not color.isValid():
            return
        for item in items:
            item.set_fill_color(color)
        self.push_history("Set fill color")

    def randomize_selected_colors(self):
        items = self._require_selection()
        if not items:
            return
        for item in items:
            item.set_random_fill_color()
        self.push_history("Randomize colors")

    def toggle_color_cycle(self):
        items = self._require_selection()
        if not items:
            return
        enable = not all(item.animate_color for item in items)
        for item in items:
            item.animate_color = enable
            item.changed.emit()
        self.push_history("Toggle color cycle")

    def toggle_pattern_animation(self):
        items = self._require_selection()
        if not items:
            return
        enable = not all(item.animate_pattern for item in items)
        for item in items:
            item.animate_pattern = enable
            item.changed.emit()
        self.push_history("Toggle moving pattern")

    def toggle_selected_borders(self):
        items = self._require_selection()
        if not items:
            return
        for item in items:
            item.toggle_stroke_visible()
        self.push_history("Toggle borders")

    def set_selected_border_color(self):
        items = self._require_selection()
        if not items:
            return
        color = QColorDialog.getColor(items[0].stroke_color, self, "Choose border color")
        if not color.isValid():
            return
        for item in items:
            item.set_stroke_color(color)
        self.push_history("Set border color")

    def delete_selected(self):
        items = self.selected_shape_items()
        if not items:
            return
        for item in items:
            self.scene.removeItem(item)
        self.push_history("Delete selected")

    def clear_all(self):
        shape_items = [i for i in self.scene.items() if isinstance(i, EditableShapeItem)]
        if not shape_items:
            return
        for item in shape_items:
            self.scene.removeItem(item)
        self.push_history("Clear all")

    def serialize_state(self) -> str:
        state = {
            "shapes": [i.to_dict() for i in self.scene.items() if isinstance(i, EditableShapeItem)],
            "tool": self.canvas.current_tool,
            "current_color": color_to_hex(self.canvas.current_color),
            "input_zoom": self.canvas.zoom_factor,
            "screen_index": self.output_window.current_screen_index,
            "output_fullscreen": self.output_window.isFullScreen(),
        }
        return json.dumps(state)

    def restore_state(self, payload: str):
        self.restoring_history = True
        state = json.loads(payload)
        for item in [i for i in self.scene.items() if isinstance(i, EditableShapeItem)]:
            self.scene.removeItem(item)
        self.scene.clearSelection()
        for shape in state.get("shapes", []):
            item = EditableShapeItem.from_dict(shape)
            self.scene.addItem(item)
            self.canvas._wire_item(item)
        self.canvas.current_color = color_from_hex(state.get("current_color", "#ffff006e"))
        tool = state.get("tool", "select")
        self.canvas.set_tool(tool)
        self.status_label.setText(
            f"Tool: { {'select':'Select / Edit','polygon':'Freehand Polygon','rect':'Rectangle','ellipse':'Circle / Ellipse'}.get(tool, tool) }"
        )
        self.canvas.reset_zoom()
        target_zoom = float(state.get("input_zoom", 1.0))
        if abs(target_zoom - 1.0) > 1e-6:
            factor = target_zoom / self.canvas.zoom_factor
            self.canvas.zoom_factor = target_zoom
            self.canvas.scale(factor, factor)
        screen_index = int(state.get("screen_index", 0))
        self.screen_combo.setCurrentIndex(max(0, min(screen_index, self.screen_combo.count() - 1)))
        self.output_window.current_screen_index = self.screen_combo.currentIndex()
        self.refresh_views()
        self.restoring_history = False

    def push_history(self, label: str):
        if self.restoring_history:
            return
        payload = self.serialize_state()
        if self.undo_stack and self.undo_stack[-1].payload == payload:
            return
        self.undo_stack.append(HistoryState(label, payload))
        if len(self.undo_stack) > MAX_HISTORY:
            self.undo_stack = self.undo_stack[-MAX_HISTORY:]
        self.redo_stack.clear()
        self.refresh_views()

    def on_history_commit_request(self, label: str):
        if label == "Pre-edit":
            # save pre-edit state only once before a drag/point edit
            self.push_history(label)
        else:
            self.push_history(label)

    def undo(self):
        if self.canvas.undo_temp_polygon():
            return
        if len(self.undo_stack) <= 1:
            return
        current = self.undo_stack.pop()
        self.redo_stack.append(current)
        self.restore_state(self.undo_stack[-1].payload)

    def redo(self):
        if self.canvas.redo_temp_polygon():
            return
        if not self.redo_stack:
            return
        state = self.redo_stack.pop()
        self.undo_stack.append(state)
        self.restore_state(state.payload)

    def save_project_dialog(self):
        start = str(self.last_saved_path) if self.last_saved_path else "projection_mapping_project.json"
        path, _ = QFileDialog.getSaveFileName(self, "Save Project", start, "JSON Files (*.json)")
        if not path:
            return
        self.save_project(Path(path))

    def save_project(self, path: Path):
        path.write_text(self.serialize_state(), encoding="utf-8")
        self.last_saved_path = path
        self.statusBar().showMessage(f"Saved project: {path}", 4000)

    def load_project_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Project", "", "JSON Files (*.json)")
        if not path:
            return
        self.load_project(Path(path))

    def load_project(self, path: Path):
        try:
            payload = path.read_text(encoding="utf-8")
            self.restore_state(payload)
            self.last_saved_path = path
            self.push_history("Load project")
            self.statusBar().showMessage(f"Loaded project: {path}", 4000)
        except Exception as exc:
            QMessageBox.critical(self, "Load failed", f"Could not load project:\n{exc}")

    def advance_animation(self):
        global GLOBAL_COLOR_PHASE, GLOBAL_PATTERN_PHASE
        GLOBAL_COLOR_PHASE = (GLOBAL_COLOR_PHASE + 2.0) % 360.0
        GLOBAL_PATTERN_PHASE = (GLOBAL_PATTERN_PHASE + 4.0) % 10000.0
        self.refresh_views()

    def closeEvent(self, event):
        self.output_window.close()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("Projection Mapping Sketch Tool")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
