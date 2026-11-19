"""Multiline prompt editor embedded in the Nuke properties pane."""
import nuke

try:
    from PySide6 import QtWidgets
except ImportError:
    from PySide2 import QtWidgets


class PromptEditor:
    def __init__(self):
        self.node = nuke.thisNode()
        self.editors = {}

    def makeUI(self):
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        for key, label, height in (("positive", "Positive", 63), ("negative", "Negative", 46)):
            editor = QtWidgets.QPlainTextEdit()
            editor.setFixedHeight(height)
            editor.setTabChangesFocus(True)
            editor.setPlainText(self.node[key].value())
            editor.textChanged.connect(lambda k=key, e=editor: self.node[k].setValue(e.toPlainText()))
            layout.addWidget(QtWidgets.QLabel(label))
            layout.addWidget(editor)
            self.editors[key] = editor
        return panel

    def updateValue(self):
        for key, editor in self.editors.items():
            value = self.node[key].value()
            if editor.toPlainText() != value:
                previous = editor.blockSignals(True)
                editor.setPlainText(value)
                editor.blockSignals(previous)
