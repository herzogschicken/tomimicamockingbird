# tomimicamockingbird

**Read along with your audiobook.** Your phone plays the audiobook. Your computer shows the
same book as text and highlights the line being read, like karaoke for books.

- It follows the real narrator's voice, not a guess based on reading speed.
- Everything runs on your own computer. Nothing is uploaded anywhere.
- Works on **Linux** and **Windows**. A graphics card makes it faster, but it is **not needed**.

---

## Contents

1. [What you need](#what-you-need)
2. [How it works, in one minute](#how-it-works-in-one-minute)
3. [Install on Linux](#install-on-linux)
4. [Install on Windows](#install-on-windows)
5. [Using it](#using-it)
6. [Let it follow your phone by itself (optional)](#let-it-follow-your-phone-by-itself-optional)
7. [How long does it take?](#how-long-does-it-take)
8. [Something went wrong](#something-went-wrong)
9. [Privacy, limits, uninstalling](#privacy)

---

## What you need

- A computer running **Linux** or **Windows 10/11**.
- About **5 GB of free disk space**.
- An **internet connection** for the first setup only.
- Your audiobook as an **.m4b** file (the usual audiobook file type).
- The same book as an **.epub** file (the usual e-book file type).
  It must be the **same edition** as the audiobook. A shortened ("abridged") audiobook will not match.
- **Optional:** an NVIDIA graphics card. Without one, everything still works, just slower.
- **Optional:** an Android phone, if you want the computer to follow your phone by itself.

> **Which audio player?** Any. You can play the audiobook on your phone, in a car, anywhere.
> The app only needs its own copy of the audio once, to "listen" to it and learn the timing.

---

## How it works, in one minute

1. You give the app the **.m4b** and the **.epub**.
2. The app listens to the whole audiobook once and writes down every word it hears, with the time.
   This is called **transcribing**. It is the slow part, and it only happens once per book.
3. The app matches what it heard to the text of the e-book. Now every word in the book has a time.
4. You open the reader, start your audiobook, and the reader highlights along with it.

---

## Install on Linux

You will type a few commands. That's all. Copy each grey box exactly, paste it, and press **Enter**.

### Step 0. Open a Terminal

Press **Ctrl + Alt + T**. If nothing happens, open your app menu and search for **Terminal**.

> To paste into a Terminal, use **Ctrl + Shift + V** (not just Ctrl + V).
> When it asks for your **password**, type it and press Enter. You won't see the letters. That's normal.

### Step 1. Install the helper programs

Pick **one** box, the one that matches your Linux.

**Ubuntu, Linux Mint, Pop!_OS, Debian, Zorin:**
```bash
sudo apt update && sudo apt install -y python3 python3-venv ffmpeg adb unzip
```

**Fedora:**
```bash
sudo dnf install -y python3 ffmpeg-free android-tools unzip
```

**Arch, Manjaro, EndeavourOS:**
```bash
sudo pacman -S --needed python ffmpeg android-tools unzip
```

Not sure which Linux you have? Type `cat /etc/os-release` and look at the `NAME=` line.

### Step 2. Download the app

1. At the top of this GitHub page, click the green **Code** button, then **Download ZIP**.
2. Unzip it into your home folder with this command:
   ```bash
   cd ~ && unzip ~/Downloads/tomimicamockingbird-main.zip
   ```
   You now have a folder called `tomimicamockingbird-main` in your home folder.

### Step 3. Graphics card (only if you have an NVIDIA card)

**No NVIDIA card, or not sure? Skip this step.** The app works without one.

Type this:
```bash
nvidia-smi
```

- If you see a table with your card's name, you're done. Go to Step 4.
- If you see "command not found", the NVIDIA driver isn't installed:
  - **Ubuntu / Mint / Pop!_OS:** open **Software & Updates**, go to the **Additional Drivers** tab,
    pick the newest "NVIDIA driver (proprietary)" and click **Apply Changes**. Restart the computer.
  - **Fedora:** follow the guide at [rpmfusion.org/Howto/NVIDIA](https://rpmfusion.org/Howto/NVIDIA). Restart the computer.

You do **not** need to install "CUDA" or "cuDNN". The app downloads the one small piece it needs by itself.

### Step 4. Start the app

```bash
cd ~/tomimicamockingbird-main && ./run.sh
```

- The **first time** takes a few minutes. It sets everything up. You'll see "Installing packages...".
- When it's ready, your web browser opens the app. If it doesn't, open your browser and go to
  **http://127.0.0.1:8765**
- **Keep the Terminal window open** while you use the app. Closing it stops the app.

### Every time after that

Open a Terminal and type the same command:
```bash
cd ~/tomimicamockingbird-main && ./run.sh
```

To stop the app, click on the Terminal window and press **Ctrl + C**.

---

## Install on Windows

### Step 0. Open the Terminal

Right-click the **Start** button and choose **Terminal** (on older Windows: **Windows PowerShell**).

> To paste into it, **right-click** inside the window, or press **Ctrl + V**.

### Step 1. Install the helper programs

Copy this whole box, paste it, and press **Enter**. It installs three free programs:
Python (runs the app), FFmpeg (reads audio files) and Android Platform Tools (talks to your phone).

```powershell
winget install -e --id Python.Python.3.12; winget install -e --id Gyan.FFmpeg; winget install -e --id Google.PlatformTools
```

- If it asks **"Do you agree to all the source agreements terms?"**, type **Y** and press Enter.
- If Windows asks **"Do you want to allow this app to make changes?"**, click **Yes**.
- When it finishes, **close the Terminal window**. (New programs only show up in a fresh window.)

> **"winget is not recognized"?** Your Windows is missing the App Installer. Install
> [App Installer from the Microsoft Store](https://apps.microsoft.com/detail/9nblggh4nns1), then try again.

### Step 2. Download the app

1. At the top of this GitHub page, click the green **Code** button, then **Download ZIP**.
2. Open your **Downloads** folder. Right-click **tomimicamockingbird-main.zip** and choose **Extract All...**
3. Choose your **Documents** folder as the place to extract to, then click **Extract**.

> Don't put it in "Program Files". Windows doesn't let apps write there.

### Step 3. Graphics card (only if you have an NVIDIA card)

**No NVIDIA card, or not sure? Skip this step.** The app works without one.

Make sure your NVIDIA driver is up to date: download it from
[nvidia.com/drivers](https://www.nvidia.com/Download/index.aspx) or use the NVIDIA app.
You do **not** need to install "CUDA" or "cuDNN". The app downloads the one piece it needs by itself.

### Step 4. Start the app

1. Open **Documents**, then the **tomimicamockingbird-main** folder.
2. **Double-click `run.bat`**.
   - If Windows shows **"Windows protected your PC"**, click **More info**, then **Run anyway**.
     (This appears for any file downloaded from the internet that isn't from a big company.)
3. A black window opens. The **first time** it takes a few minutes to set everything up.
4. When it's ready, your web browser opens the app. If it doesn't, go to **http://127.0.0.1:8765**
5. **Keep the black window open** while you use the app. Closing it stops the app.

> **Starting it from the Terminal instead?** Go into the app folder and type `.\run.bat`
> (with the dot and backslash in front). Typing just `run.bat` gives a "not recognized" error.

> **Tip:** right-click `run.bat`, choose **Show more options > Send to > Desktop (create shortcut)**.
> Now you can start the app from your desktop.

### Every time after that

Double-click `run.bat` (or your desktop shortcut). To stop the app, close the black window.

---

## Using it

### 1. Add a book

On the start page, under **Add a book**:

- Drag the **.epub** onto the left box and the **.m4b** onto the right box, **or**
- type where the files are, like `~/Audiobooks/My Book.m4b` (Linux) or
  `C:\Users\You\Music\My Book.m4b` (Windows).
  Tip for Windows: hold **Shift**, right-click the file, choose **Copy as path**, and paste it.

Then click **Add book**.

> Typing the location is better for big audio files. Dragging makes a copy; typing doesn't.

### 2. Transcribe it (once per book)

Click **Transcribe** on the book. A progress bar shows how far along it is and how long is left.

- You can keep using your computer. You can also **Pause** and come back later.
- If you close the app or turn off the computer, it **picks up where it stopped** next time.
- The first time, it downloads the speech model (500 MB to 1.6 GB). Only once.

When it's done, the app matches the words to the book by itself. The book then shows **Ready**.

> **"Needs review"?** Some chapters didn't match well. Usually that's an intro or credits, or a
> different edition. Click **Details** to see which chapters. It still works for the rest.

### 3. Read along

Click **Read** on the book. Then choose how it should keep up with your audiobook:

**A. By hand (works with any player, anywhere)**
1. Pick the chapter you're on from the menu at the top.
2. Press **play** on your audiobook and press the **Space bar** on the computer at the same moment.
3. If it gets ahead or behind, **click the line you're hearing**. It jumps there. That's it.

Handy keys: **Space** = pause/play, **Left / Right arrows** = 5 seconds back/forward,
**Up / Down arrows** = one line back/forward. Set the **Speed** box to the same speed as your player.

**B. Automatically, from your Android phone**
The reader follows your phone by itself: pause, skip, speed, everything.
See the next section to set it up.

### 4. Make it comfortable

Click **Aa & sync** in the reader for text size, width, font, line spacing, and colours
(Old book, Dark, Sepia, Light). **Save current look** keeps a set you like.
Click **Full screen** (or press **F**) to hide everything but the text.

**Highlights:** select some text with the mouse and pick a colour. Click **Highlights** to see them
all, add notes, or save them as a file.

---

## Let it follow your phone by itself (optional)

This needs an **Android** phone and a **USB cable**. iPhones can't do this.

### One-time phone setup

1. On the phone, open **Settings > About phone**.
2. Find **Build number** and **tap it 7 times**. You'll see "You are now a developer!".
   (On some phones it's under **Settings > About phone > Software information**.)
3. Go back to **Settings > System > Developer options** (or search Settings for "Developer options").
4. Turn on **USB debugging**.
5. Plug the phone into the computer with the cable.
6. The phone asks **"Allow USB debugging?"**. Tick **Always allow from this computer** and tap **Allow**.

### Use it

1. Start your audiobook on the phone.
2. Open the reader on the computer. The badge at the top says **Following phone**.

That's all. If you unplug the phone, the reader keeps going on its own clock from the same spot,
and takes over again when you plug it back in.

> **Which player apps work?** It's tested with **Smart Audiobook Player**. Other players that show
> play/pause controls in the notification bar should work too, but haven't been tested.

### No cable (Wi-Fi)

With the cable plugged in, open a Terminal and type:
```
adb tcpip 5555
```
Unplug the cable. Find your phone's address in **Settings > About phone > IP address**
(it looks like `192.168.1.23`). In the reader, open **Aa & sync**, type that address followed by
`:5555` (for example `192.168.1.23:5555`) and click **Connect**.
You need to repeat the `adb tcpip 5555` step after the phone restarts.

### Import books straight from the phone

If the audiobooks are on your phone, you don't need to copy them yourself. With the phone
connected, click **Scan phone** on the start page. Pick a book and click **Import**.
The app copies the audio, transcribes it, and then **deletes its copy** to save space.
Your phone's files are never changed.

---

## How long does it take?

Transcribing is a one-time job per book. Rough times for a **10-hour** audiobook:

| Your computer | What the app uses | Rough time |
|---|---|---|
| NVIDIA card (6 GB or more) | large-v3-turbo model | well under an hour |
| Modern CPU, no NVIDIA card | small model | about 2 to 4 hours |
| Older or laptop CPU | small model | longer; try the "base" model |

These are estimates. The app shows the real speed while it works. One measured example:
a 4.7-hour book took 15 minutes with the bigger large-v3 model on an RTX 3060.

You can change the model under **Transcription settings** at the bottom of the start page.
Leave it on **auto** unless you have a reason.

---

## Something went wrong

**"Python was not found" / "not recognized".**
- Linux: do Step 1 again.
- Windows: do Step 1 again. Then **sign out of Windows and sign back in** (or restart), and
  double-click `run.bat` again. Windows only tells programs about newly installed software after that.
- Windows, from the Terminal: type `.\run.bat`, not `run.bat`.

**The start page says "ffprobe missing" or "no AAC decoder".**
FFmpeg is missing. Do Step 1 again.
On Fedora, if it says "no AAC decoder", install the full FFmpeg from RPM Fusion:
```bash
sudo dnf install -y https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm
sudo dnf swap -y ffmpeg-free ffmpeg --allowerasing
```

**The start page says "GPU not usable".**
Without an NVIDIA card this is normal, and the app uses the CPU. With one, update the NVIDIA
driver (Step 3) and restart. To test the card, open a Terminal in the app folder and type:
Linux `.venv/bin/python tools/gpu_check.py`, Windows `.\.venv\Scripts\python tools\gpu_check.py`

> **Open a Terminal in the app folder:** on Linux type `cd ~/tomimicamockingbird-main`.
> On Windows, open the folder in File Explorer, right-click an empty spot and choose **Open in Terminal**.

**The phone isn't found.**
Type `adb devices` in a Terminal. If the list is empty, check the cable (some cables only charge)
and that USB debugging is on. If it says `unauthorized`, look at the phone and tap **Allow**.

**"Address already in use" when starting.**
The app is already running in another window. Or start it on another port:
Linux `PORT=9000 ./run.sh`. Windows: open the Terminal in the app folder (see below) and type
`$env:PORT=9000; .\run.bat`

**The highlight is a few lines off.**
Click the line you're hearing. If it's off in one whole chapter, open the book's **Details**
page, click that chapter, and look at the coloured text. Faded words had no match. Use **Map**
to point the chapter at the right part of the book.

**Something else.** Open an issue on GitHub and copy in the red error text from the app or the
Terminal window.

---

## Privacy

Everything stays on your computer. The app only goes online to install itself the first time and to
download the speech model once (from Hugging Face). Your books, highlights and reading positions are
stored here:

- Linux: `~/.local/share/tomimicamockingbird`
- Windows: `%LOCALAPPDATA%\tomimicamockingbird` (paste that into the File Explorer address bar)

## Known limits

- Without the phone link, the computer can't see you pause or skip on your player. Press Space or
  click a line to catch up.
- The e-book and the audiobook must be the same edition. Big differences are flagged, not guessed.
- Unusual names are often misheard. The matching is built to cope with that.
- Supports .m4b and .m4a audiobooks with chapters, and .epub books. No MP3 folders or PDFs yet.

## Uninstalling

Delete the app folder and the data folder listed under [Privacy](#privacy). That's everything.

## For the curious

How the matching and the phone sync work: [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md).

## License

MIT. See [LICENSE](LICENSE). Use it, change it, share it.
