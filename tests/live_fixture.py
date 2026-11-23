"""A real Win32 window emitting its own sound, for process-targeted acceptance."""

import sys
import tkinter as tk
import winsound

root = tk.Tk()
root.title("HanHua Capture Fixture")
root.geometry("700x310+40+65")
root.configure(bg="#263345")
label = tk.Label(root, text="Open the north gate", font=("Arial", 31), bg="#263345", fg="white")
label.pack(expand=True)
counter = [0]


def change():
    counter[0] += 1
    label.config(text=("Open the north gate", "Take the silver key", "Follow the guard")[counter[0] % 3])
    root.after(8500, change)


root.after(7000, change)
root.after(90000, root.destroy)
winsound.PlaySound(sys.argv[1], winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
try:
    root.mainloop()
finally:
    winsound.PlaySound(None, 0)
