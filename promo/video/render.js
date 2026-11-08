// Render index.html frame by frame into a video.
//
//   node render.js frames <out-dir> <t1> <t2> ...   still frames as PNG, for checking
//   node render.js video <out.mp4> [fps]             the whole video (needs ffmpeg)
//
// CUT=web renders the website cut (no title or outro, slower, loops seamlessly).
//
// Needs puppeteer-core and a Chromium at $CHROME (default /usr/bin/chromium).
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const puppeteer = require("puppeteer-core");

const CUT = process.env.CUT || "";
const PAGE = "file://" + path.join(__dirname, "index.html") + "?render" + (CUT ? "&cut=" + encodeURIComponent(CUT) : "");
const LOOP_FADE = 0.7;  // seconds of the end blended into the start, for the looping cut

async function open() {
  const browser = await puppeteer.launch({
    executablePath: process.env.CHROME || "/usr/bin/chromium",
    headless: true,
    args: ["--no-sandbox", "--force-color-profile=srgb", "--hide-scrollbars", "--font-render-hinting=none"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1920, height: 1080, deviceScaleFactor: 1 });
  await page.goto(PAGE, { waitUntil: "networkidle0", timeout: 60000 });
  await page.evaluate(() => window.ready);
  return { browser, page };
}

async function frames(dir, times) {
  fs.mkdirSync(dir, { recursive: true });
  const { browser, page } = await open();
  for (const t of times) {
    await page.evaluate((t) => window.render(window.storyTime(t)), t);
    await page.screenshot({ path: path.join(dir, `t${t.toFixed(2)}.png`) });
  }
  await browser.close();
}

async function video(out, fps) {
  const { browser, page } = await open();
  const duration = await page.evaluate(() => window.DURATION);
  const total = Math.round(duration * fps);
  const loop = CUT === "web";
  const raw = loop ? out.replace(/\.mp4$/, "") + ".raw.mp4" : out;
  const encode = ["-c:v", "libx264", "-preset", "slow", "-crf", "17", "-pix_fmt", "yuv420p", "-profile:v", "high",
    "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-movflags", "+faststart"];
  const ff = spawn("ffmpeg", [
    "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", String(fps), "-c:v", "png", "-i", "-",
    ...(loop ? ["-c:v", "libx264", "-preset", "fast", "-crf", "8", "-pix_fmt", "yuv420p"] : encode), raw,
  ], { stdio: ["pipe", "inherit", "inherit"] });
  const started = Date.now();
  for (let i = 0; i < total; i++) {
    await page.evaluate((t) => window.render(window.storyTime(t)), i / fps);
    const buf = await page.screenshot({ type: "png" });
    if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once("drain", r));
    if (i % 60 === 0) process.stderr.write(`frame ${i}/${total}  ${((Date.now() - started) / 1000).toFixed(0)}s\n`);
  }
  ff.stdin.end();
  await new Promise((r) => ff.on("close", r));
  await browser.close();
  if (loop) {
    // Blend the last LOOP_FADE seconds into the first ones: the result starts and ends on the same frame.
    const d = total / fps, f = LOOP_FADE;
    const graph = `[0]split[x][y];[x]trim=start=${f},setpts=PTS-STARTPTS[a];[y]trim=end=${f},setpts=PTS-STARTPTS[b];` +
      `[a][b]xfade=transition=fade:duration=${f}:offset=${(d - 2 * f).toFixed(4)}`;
    await new Promise((resolve, reject) => {
      const p = spawn("ffmpeg", ["-y", "-loglevel", "error", "-i", raw, "-filter_complex", graph, ...encode, out],
                      { stdio: "inherit" });
      p.on("close", (code) => (code === 0 ? resolve() : reject(new Error("ffmpeg loop pass failed"))));
    });
    fs.unlinkSync(raw);
  }
}

(async () => {
  const [mode, target, ...rest] = process.argv.slice(2);
  if (mode === "frames") await frames(target, rest.map(Number));
  else if (mode === "video") await video(target, Number(rest[0] || 60));
  else { console.error("usage: node render.js frames <dir> <t...> | video <out.mp4> [fps]"); process.exit(2); }
})();
