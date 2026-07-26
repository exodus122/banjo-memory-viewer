"""
main.py - Banjo Memory Viewer entry point
Reads N64 RDRAM from BizHawk, or Xbox 360 memory from Xenia-canary, via
Windows ReadProcessMemory. Windows only.
"""
import sys
import tkinter as tk
from trainer_app import TrainerApp

if __name__ == "__main__":
    root = tk.Tk()
    app  = TrainerApp(root)
    root.mainloop()
