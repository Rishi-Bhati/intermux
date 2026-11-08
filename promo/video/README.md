# InterMux promo video

An 18-second motion demo of the GUI, for the README and for ads, with a looping cut for the website. It is not a screen recording. `index.html` rebuilds the 2.1.0 main window in HTML, using the colours, layout and text of `intermux/ui/`. A virtual cursor and camera then drive it through a scripted session with made-up data. Each frame is a pure function of time, so a render always produces the same video.

| File | What it is |
|------|------------|
| `index.html` | The video: scenes, timeline, camera, cursor and captions. Open it in a browser to watch it loop, or add `?t=7.5` to freeze one frame. |
| `render.js` | Renders `index.html` frame by frame through headless Chromium and pipes the frames to ffmpeg. |
| `mock_app.py` | Runs the real Tk GUI with fake interfaces, bindings and traffic, without touching the system. Use it to compare the HTML window against the app. |

## Render

```bash
cd promo/video
npm install                     # puppeteer-core only; it uses your system Chromium
node render.js video out/intermux-demo.mp4 60
node render.js frames out/frames 3.7 9.8 14.8   # still frames, for checking
CUT=web node render.js video out/intermux-demo-loop.mp4 60   # website cut
```

The website cut (`?cut=web` in the browser) leaves out the title and the closing wordmark, since the page around it already shows them. It plays the steps 1.3 times slower and, at seven key moments, slows almost to a freeze: the element being used is spotlit and a note explains it (`HOLDS` in `index.html`). It starts and ends on the same wide shot and blends its last 0.7 seconds into its first, so it loops without a jump. It runs about 38 seconds.

| Pause | Note |
|-------|------|
| wlan0 in the Interface list | Pick the connection |
| Firefox in the app picker | Pick the app |
| Cloudflare in the DNS list | Give it its own DNS |
| Firefox's row in the Queue | Line up your apps |
| The password prompt | Start them all at once |
| The live Bindings rows | Every app, its own lane |
| wlan0's public IP | See where it comes out |

The README uses `docs/media/intermux-demo.webp` and `docs/media/intermux-demo.mp4`, made from a clean render of the full cut. `CLEAN=1` leaves out the film grain and the background drift, which otherwise change every pixel of every frame and turn into blocky noise when compressed:

```bash
CLEAN=1 node render.js video out/intermux-demo-clean.mp4 60
cp out/intermux-demo-clean.mp4 ../../docs/media/intermux-demo.mp4
ffmpeg -i out/intermux-demo-clean.mp4 -vf "fps=30,scale=1600:-1:flags=lanczos" -c:v libwebp_anim -quality 80 -compression_level 4 -loop 0 ../../docs/media/intermux-demo.webp
```

Set `CHROME` if Chromium is not at `/usr/bin/chromium`. The page loads Schibsted Grotesk and JetBrains Mono from Google Fonts and uses the local Noto Sans for the app window, so render with network access on a machine that has Noto Sans.

## Compare with the real app

```bash
Xvfb :97 -screen 0 1600x1200x24 &
DISPLAY=:97 WAYLAND_DISPLAY= python3 promo/video/mock_app.py --scene bindings
DISPLAY=:97 WAYLAND_DISPLAY= PROMO_OPEN=picker python3 promo/video/mock_app.py
```

## Story

| Time | Scene |
|------|-------|
| 0–2.5 s | Title: "Pick a network for every app." |
| 2.5–4.4 s | 01 Choose a connection: the Interface list, wlan0 picked |
| 4.4–7.6 s | 02 Pick any app: Choose…, Firefox, DNS set to Cloudflare, Add to queue |
| 7.6–10.8 s | 03 Start them all: Start all, one password prompt |
| 10.8–13.5 s | 04 Each app gets its own lane: live Bindings table |
| 13.5–15.3 s | 05 Check where it exits: Check public IP on wlan0 |
| 15.3–18 s | Wordmark, tagline and the install command |

All addresses are private or documentation ranges (192.168.x, 172.20.10.x, 198.51.100.24).
