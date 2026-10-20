# Offline Doc Tools: start here

Converts Word documents (`.docx`) to PDF **on your own computer**. Your documents are never sent
anywhere.

## What you need

* Windows 10 or 11, 64-bit.
* **LibreOffice**, installed separately (free, from libreoffice.org). The program uses it to make
  the PDFs. It was tested with LibreOffice 26.2.1; other versions may work but are not tested. If it
  is missing, the program tells you so when it starts.
* **Microsoft Visual C++ Redistributable (x64), version 14.44 or newer** (free, from Microsoft).
  Many computers already have it. If the program does not start and Windows mentions
  `VCRUNTIME140.dll` or `MSVCP140.dll`, install it and try again.

## Install

1. Unzip the **whole** package into a folder where you can write files, for example
   `C:\Users\<you>\OfflineDocTools`. Not into `Program Files`: updates need to write to the folder
   that contains the program.
2. Keep the folder together. Start `OfflineDocTools.exe` inside it.
3. The program is not code-signed, so Windows may show *"Windows protected your PC"*. If you trust
   where you got the package, choose *More info* and then *Run anyway*.

## Use

1. *Add files…* and choose the `.docx` files.
2. *Choose…* an output folder. PDFs are saved there; an existing file is never overwritten (a new
   name is used instead). Your original documents are never changed.
3. *Convert to PDF*. The table shows what happened to each file.

Pictures that a document only *links to* (by web address or file path) are left out of the PDF and
never fetched; the table tells you how many. A document that contains an embedded web page or a
picture that points at other files is refused with a message, instead of being converted.

## Updates (only if your supplier has set them up)

**In this version, updates are not set up and the update buttons are not shown.** Install any new
version by unzipping the package your supplier sends you. The rest of this section applies only to a
build in which your supplier has set updates up; then the buttons appear at the bottom of the window.

Nothing happens unless you click. **Check for updates** asks a private repository whether a newer
version exists. The first time it asks for an **update access token** that your supplier gives you;
it is stored encrypted for your Windows account only. Nothing about you or your documents is sent.
If a newer version exists, **Download and install…** asks you to confirm. The program then closes,
replaces itself, and starts again; if the new version does not start, the previous one is restored
automatically. The result is shown the next time the window opens. If the program has closed and does
not reopen by itself within a minute or two, start it again: it will tell you what happened. Do not
update while converting.

If an update is ever interrupted by a power failure and the program will not start, look next to the
program folder for a folder named `OfflineDocTools.backup-…`: rename it to `OfflineDocTools`.

## Logs and privacy

`%LOCALAPPDATA%\OfflineDocTools\logs\` holds `app.log` (and `update.log` after an update). They contain
no document names or contents. Converting documents uses no network. The only feature that can use
the network is the optional update feature, which is not set up in this version and, where it is, runs
only when you click its buttons.

## Open-source components

This program contains open-source software, including Qt and PySide6 (used under the GNU LGPL
version 3), OpenSSL, and Python. The **About and licences…** button in the program window says so too.
Their notices and licence texts are in `THIRD-PARTY-NOTICES.txt`
(next to the program) and in the `licenses` folder, together with the addresses where the source code
of the Qt and PySide6 versions used is published. The terms for using Offline Doc Tools itself are
provided separately by your supplier.
