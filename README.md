# InkCast: draw on your Windows screen from an iPad or Android tablet

![InkCast: draw live on your Windows screen from an iPad or Android tablet](screenshots/banner.png)

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
![Platform: Windows](https://img.shields.io/badge/PC-Windows%2010%20%7C%2011-0078d4)
![Tablets: iPad and Android](https://img.shields.io/badge/tablet-iPad%20%7C%20Android-34c759)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-ffcc00)

**InkCast is a free, open-source screen annotation tool that turns your iPad or Android tablet into a wireless drawing pad for your Windows PC.** Write, highlight, draw arrows and point with a laser on top of anything on your screen (slides, code, browser, PDFs, videos) using an Apple Pencil, Samsung S Pen, any stylus, or just your finger.

Your PC gets an invisible, click-through ink layer on top of everything. Open a page in your tablet's browser, and whatever you draw there shows up on the PC instantly, while your mouse and keyboard keep working normally underneath. Screen recorders and streaming software like OBS capture the ink, which makes it ideal for teaching, online classes, tutorial videos and live streams.

No tablet app to install. No subscription. No account.

![Annotating a lesson slide live from a tablet](screenshots/annotate.png)

## Who it's for

- **Teachers and tutors** recording lessons or teaching online over Zoom, Google Meet or Teams
- **YouTubers and course creators** who want to draw on screen while recording
- **Streamers** who want to sketch, circle and point during a live stream
- **Anyone presenting** who wants a laser pointer and whiteboard without buying a drawing tablet

## Features

- **Pens:** pen, marker, highlighter, neon (glowing ink) and a laser pointer that fades away on its own
- **Shapes:** line, arrow, **curved arrow**, box, circle
- **Text** in three fonts (sans, handwriting, serif); tap existing text to edit it
- **Images:** pick from your tablet, paste from the clipboard, drag and drop, or copy an image on the PC and press `Ctrl+Alt+V`
- **Select:** tap one thing, or draw a loop around several. Then move them, resize them from the corner dots, or change their color, size or font together
- **Whiteboard:** switch between your screen and a blank board (white, grid, black or green chalkboard); each keeps its own drawings
- **Undo / redo, eraser, clear**
- **Works with fingers or a pencil.** When a pencil is detected, fingers stop drawing automatically so your palm can rest on the screen (toggle with **Fingers**)
- **Live screen preview** on the tablet so you can see where you're drawing
- **PC keyboard shortcuts** so you can control it without touching the tablet

| Loop to select | Edit many things at once |
|---|---|
| ![Lasso selection](screenshots/lasso.png) | ![Selected items recolored](screenshots/select.png) |

![Whiteboard mode](screenshots/whiteboard.png)

## Requirements

- Windows 10 or 11
- [Python 3.10+](https://www.python.org/downloads/) (tick **"Add python.exe to PATH"** during install)
- A tablet (iPad or Android) on the **same WiFi network** as the PC, using Safari, Chrome or another modern browser

## Getting started

1. Download this repository (green **Code** button → **Download ZIP**) and unzip it.
2. Double-click **`Start InkCast.bat`**. The first run installs what it needs (PySide6, aiohttp, mss, Pillow), which takes a minute.
3. If Windows asks about the firewall, click **Allow**.
4. The window shows an address like `http://192.168.0.105:8765`. Open it in your tablet's browser.
5. Optional: add it to your home screen (Safari: **Share → Add to Home Screen**; Chrome: **⋮ → Add to Home screen**) so it opens full screen like an app.

Or run it directly:

```bash
pip install -r requirements.txt
python inkcast.py
```

## PC shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+Alt+Z` / `Ctrl+Alt+Y` | Undo / Redo |
| `Ctrl+Alt+C` | Clear ink |
| `Ctrl+Alt+H` | Hide / show ink |
| `Ctrl+Alt+L` | Laser pointer on / off |
| `Ctrl+Alt+W` | Whiteboard on / off |
| `Ctrl+Alt+B` | Change whiteboard background |
| `Ctrl+Alt+V` | Paste the image you copied on the PC |

The tray icon (red circle near the clock) also has Paste, Clear, Whiteboard, Hide and Quit.

## Pasting images

- **On the PC:** right-click any image in your browser → **Copy image**, then press `Ctrl+Alt+V`. It appears in the middle of the screen, already selected so you can drag and resize it.
- **On the tablet:** copy an image in your browser, tap **Paste** in the tool bar, then long-press the box and choose **Paste**. If only a link was copied, the PC downloads the image for you.
- You can also tap **Image** to pick from your photos, or drag an image onto the page.

## Tips

- For the lowest lag, connect the PC to the router with a cable and put the tablet on 5 GHz WiFi.
- Turn off any VPN on the PC; it can stop the tablet from reaching it.
- **Fingers** on = fingers draw. Off = only the pencil draws. It switches off automatically the first time a pencil is used.
- **Preview** turns the live PC picture on the tablet on or off. **Screen** and **Board** switch where you're drawing.
- The ink draws on your primary monitor.

## How InkCast compares

| | InkCast | Screen-drawing apps (Epic Pen, ZoomIt, gInk) | Tablet-as-monitor apps (Spacedesk, Duet, Astropad) |
|---|---|---|---|
| Draw from a tablet | Yes | No, you draw with the PC's mouse or pen | Yes |
| Mouse keeps working while you draw | Yes, the tablet has its own ink layer | No, drawing takes over the cursor | No, the pencil moves the PC's cursor |
| Works on iPad and Android | Both | n/a | Varies by app |
| Price | Free and open source | Free / paid | Free to subscription |

## FAQ

**Can I use my iPad as a drawing tablet for Windows?**
Yes. Run InkCast on the PC, open the address it shows in Safari on the iPad, and draw with an Apple Pencil (or your finger). The ink appears on your Windows screen live.

**Does it work with Android tablets and the Samsung S Pen?**
Yes. Open the page in Chrome on any Android tablet. S Pen, other styluses and fingers all work.

**Does the ink show up in OBS, Zoom or screen recordings?**
Yes. The ink is drawn on your real screen, so anything that captures the screen (OBS, Streamlabs, Zoom, Teams, Google Meet, the Xbox Game Bar recorder) captures it too.

**Do I need to install an app on the tablet?**
No. It runs in the tablet's browser. You can add it to the home screen to open it full screen like an app.

**Does it need the internet?**
Only for the first install. After that the PC and tablet talk directly over your local WiFi; nothing is sent to the cloud. (Pasting an image link is the exception: the PC downloads that image.)

**Is there lag?**
Very little on a normal home network. Only pen coordinates travel over WiFi, not video. A cable from the PC to the router and 5 GHz WiFi on the tablet give the best results.

**Can I use it as a whiteboard?**
Yes. Tap **Board** for a white, grid, black or green chalkboard background, and tap **Screen** to go back. Each keeps its own drawings.

**Does it work on Mac or Linux?**
The PC side is built and tested for Windows 10 and 11. Most of it is cross-platform Python, but keyboard shortcuts and fonts are Windows-specific for now.

## How it works

`inkcast.py` runs three things:

1. A transparent, always-on-top window that ignores mouse input and renders the ink with Qt. Only the part of the screen that changed is repainted.
2. A small web server that serves the tablet page and receives pen input over a WebSocket. Strokes are sent the moment they happen, and moving or resizing a selection sends a single tiny transform instead of all the points.
3. A screen capture that sends a low-resolution preview back to the tablet. It slows down while you draw so the pen gets the network first.

Text uses the same font files on both ends (the PC shares its fonts with the tablet), so text looks the same in both places.

## License

[Apache License 2.0](LICENSE)
