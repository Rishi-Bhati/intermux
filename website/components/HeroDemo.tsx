"use client";

import { useEffect, useRef, useState } from "react";
import { Maximize2, Pause, Play } from "lucide-react";
import usePrefersReducedMotion from "@/hooks/usePrefersReducedMotion";
import styles from "./HeroDemo.module.css";

/**
 * The walkthrough video beside the hero copy. It loops muted while it is on screen and pauses when it
 * scrolls away. With reduced motion it waits on its first frame until it is played.
 */
export default function HeroDemo() {
  const ref = useRef<HTMLVideoElement>(null);
  const reduced = usePrefersReducedMotion();
  const [playing, setPlaying] = useState(false);
  const [userPaused, setUserPaused] = useState(false);

  useEffect(() => {
    const video = ref.current;
    if (!video) return;
    if (reduced || userPaused) {
      video.pause();
      return;
    }
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) video.play().catch(() => {});
        else video.pause();
      },
      { threshold: 0.25 },
    );
    observer.observe(video);
    return () => observer.disconnect();
  }, [reduced, userPaused]);

  const toggle = () => {
    const video = ref.current;
    if (!video) return;
    if (video.paused) {
      setUserPaused(false);
      video.play().catch(() => {});
    } else {
      setUserPaused(true);
      video.pause();
    }
  };

  const enlarge = () => {
    const video = ref.current;
    if (!video) return;
    video.play().catch(() => {});
    video.requestFullscreen?.().catch(() => {});
  };

  return (
    <figure className={styles.demo}>
      <div className={styles.frame}>
        <video
          ref={ref}
          className={styles.video}
          muted
          loop
          playsInline
          preload="metadata"
          poster="/video/intermux-demo-poster.jpg"
          aria-label="InterMux walkthrough: pick a connection, pick an app, start them all with one password, and see each app on its own lane"
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onClick={toggle}
        >
          <source src="/video/intermux-demo.webm" type="video/webm" />
          <source src="/video/intermux-demo.mp4" type="video/mp4" />
        </video>
        {!playing && (
          <button type="button" className={styles.bigPlay} onClick={toggle} aria-label="Play the walkthrough">
            <Play size={26} strokeWidth={2} aria-hidden="true" />
          </button>
        )}
      </div>
      <figcaption className={styles.bar}>
        <span className={styles.live} data-on={playing || undefined} aria-hidden="true" />
        <span className={styles.label}>Walkthrough</span>
        <span className={styles.meta}>38 s · made-up data</span>
        <span className={styles.spacer} />
        <button type="button" className={styles.icon} onClick={toggle} aria-label={playing ? "Pause" : "Play"}>
          {playing ? <Pause size={14} strokeWidth={2.2} aria-hidden="true" /> : <Play size={14} strokeWidth={2.2} aria-hidden="true" />}
        </button>
        <button type="button" className={styles.icon} onClick={enlarge} aria-label="Watch full screen">
          <Maximize2 size={14} strokeWidth={2.2} aria-hidden="true" />
        </button>
      </figcaption>
    </figure>
  );
}
