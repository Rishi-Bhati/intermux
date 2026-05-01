import sys
import os
import subprocess
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import tkinter as tk
from tkinter import *
import core.interface as interface
from tkinter import messagebox, Toplevel

root = tk.Tk()
root.title("Interfaces")
root.geometry("700x600")
root.minsize(400, 300)
root.configure(bg='#2E3436')

# Make all columns expand equally
root.grid_columnconfigure(0, weight=1)
root.grid_columnconfigure(1, weight=1)
root.grid_columnconfigure(2, weight=1)
root.grid_columnconfigure(3, weight=1)


#--------HELPERS--------

_VIRTUAL_PREFIXES = ("veth", "br-", "docker", "tun", "tap", "virbr", "vmnet", "vboxnet")

def _is_real_interface(name: str) -> bool:
    """Return True only for physical/real interfaces (Wi-Fi, Ethernet, USB, etc.)."""
    return not name.startswith(_VIRTUAL_PREFIXES)

def _format_iface(iface: dict) -> str:
    ip = iface['ip_addresses'][0] if iface['ip_addresses'] else 'N/A'
    return f"{iface['name']} - {iface['flag']} - {iface['type']} - {ip}"


#--------FUNCTIONS--------

def refresh():
    interface_list.delete(0, END)
    interfaces_list = interface.get_active_interfaces()
    real = [i for i in interfaces_list if _is_real_interface(i['name'])]
    if not real:
        interface_list.insert(END, "No active interfaces found.")
    else:
        for iface in real:
            interface_list.insert(END, _format_iface(iface))

def open_app_window():
    script_path = os.path.join(os.path.dirname(__file__), 'app.py')
    env = os.environ.copy()
    subprocess.Popen([sys.executable, script_path], env=env)

def routing():
    open_app_window()


#--------LABELS--------
header = Label(root, text='Available Interfaces', bg='#2E3436', fg='white', font=('Arial', 16))
header.grid(row=0, column=1, pady=10, padx=10, sticky='n')


#--------BUTTONS--------

refresh_button = Button(root, text='Refresh', command=refresh, bg='#555753', fg='white',
                        activebackground='#555753', activeforeground='white')
refresh_button.grid(row=0, column=2, sticky='e', padx=10, pady=10)

routing_button = Button(root, text='Create routing tables', command=routing, bg='#555753', fg='white',
                        activebackground='#555753', activeforeground='white')
routing_button.grid(row=2, column=1, sticky='e', padx=10, pady=10)


#--------LISTBOX--------

interface_list = Listbox(root, bg='#555753', fg='white', selectbackground='#555753',
                         selectforeground='white', width=50, height=10)

interfaces_list = interface.get_active_interfaces()
real_interfaces = [i for i in interfaces_list if _is_real_interface(i['name'])]
if not real_interfaces:
    interface_list.insert(END, "No active interfaces found.")
for iface in real_interfaces:
    interface_list.insert(END, _format_iface(iface))
interface_list.grid(row=1, column=0, columnspan=4, pady=20)

root.mainloop()