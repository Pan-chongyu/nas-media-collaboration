import tkinter as tk
import unittest
from unittest.mock import patch

from file_transfer import DragDropRoot, file_drag_payload, register_file_drag


class FileDragTests(unittest.TestCase):
    def test_windows_paths_round_trip_through_tcl_without_network_io(self):
        paths = (r"\\SmartStorage\素材库\采访 01\片段 {一}.mov", r"C:\素材\第二条 [版本2].mp4")
        tcl = tk.Tcl()
        with patch("pathlib.Path.stat", side_effect=AssertionError("Drag must not access SMB")):
            payload = file_drag_payload(paths)
            tcl.createcommand("drag_payload", lambda: payload)
            # Read exactly the nested Tcl list that TkDND receives from Python.
            encoded = tcl.call("drag_payload")
            self.assertEqual(tcl.splitlist(encoded)[0:2], ("copy", "DND_Files"))
            self.assertEqual(tcl.splitlist(tcl.splitlist(encoded)[2]), paths)
            tcl.tk.deletecommand("drag_payload")

    def test_rejects_empty_relative_and_invalid_paths(self):
        for paths in ([], ["file.mov"], [r"C:relative.mov"], ["C:\\bad\0.mov"]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                file_drag_payload(paths)

    def test_native_drag_registration_and_cleanup(self):
        root = DragDropRoot()
        root.withdraw()
        try:
            self.assertTrue(root.dnd_version, "Install requirements.txt including tkinterdnd2")
            label = tk.Label(root, text="素材")
            self.assertTrue(register_file_drag(label, lambda _: [r"\\SmartStorage\素材库\片段.mov"]))
            self.assertTrue(label.dnd_bind("<<DragInitCmd>>"))
            self.assertIn("TkDND_Drag1", label.bindtags())
            label.destroy()
            root.update_idletasks()
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
