// Встраивается в страницу вебинара до её собственных скриптов.
// Собирает весь входящий звук (WebRTC-потоки и <audio>/<video>) в один поток и пишет его MediaRecorder'ом.
// Куски записи отдаются в Python через window.__stCaptureChunk(base64).
(() => {
  if (window.__stCapture) return;
  const st = (window.__stCapture = { tracks: new Set(), media: new WeakSet(), started: false, bytes: 0 });

  let ctx, dest, recorder;
  function ensureRecorder() {
    if (st.started) return;
    ctx = new AudioContext();
    dest = ctx.createMediaStreamDestination();
    recorder = new MediaRecorder(dest.stream, { mimeType: "audio/webm;codecs=opus", audioBitsPerSecond: 48000 });
    recorder.ondataavailable = async (e) => {
      if (!e.data || !e.data.size || !window.__stCaptureChunk) return;
      const buf = new Uint8Array(await e.data.arrayBuffer());
      let bin = "";
      for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
      st.bytes += buf.length;
      window.__stCaptureChunk(btoa(bin));
    };
    recorder.start(5000);
    st.started = true;

    // Уровень громкости — чтобы Python понял, что эфир затих и запись можно заканчивать.
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    ctx.createMediaStreamSource(dest.stream).connect(analyser);
    const data = new Float32Array(analyser.fftSize);
    st.lastSound = Date.now();
    setInterval(() => {
      analyser.getFloatTimeDomainData(data);
      let sum = 0;
      for (const v of data) sum += v * v;
      st.level = Math.sqrt(sum / data.length);
      if (st.level > 0.003) st.lastSound = Date.now();
    }, 1000);
  }

  function addTrack(track) {
    if (!track || track.kind !== "audio" || st.tracks.has(track.id)) return;
    try {
      ensureRecorder();
      if (ctx.state === "suspended") ctx.resume();
      ctx.createMediaStreamSource(new MediaStream([track])).connect(dest);
      st.tracks.add(track.id);
    } catch (e) { console.warn("stCapture track", e); }
  }

  function addMedia(el) {
    if (st.media.has(el)) return;
    const hook = () => {
      try {
        const s = el.captureStream ? el.captureStream() : null;
        if (!s) return;
        s.getAudioTracks().forEach(addTrack);
        s.addEventListener("addtrack", (e) => addTrack(e.track));
        st.media.add(el);
      } catch (e) { console.warn("stCapture media", e); }
    };
    if (el.readyState >= 1) hook(); else el.addEventListener("loadedmetadata", hook, { once: true });
  }

  const NativePC = window.RTCPeerConnection;
  if (NativePC) {
    window.RTCPeerConnection = function (...args) {
      const pc = new NativePC(...args);
      pc.addEventListener("track", (e) => addTrack(e.track));
      return pc;
    };
    window.RTCPeerConnection.prototype = NativePC.prototype;
    Object.setPrototypeOf(window.RTCPeerConnection, NativePC);
  }

  setInterval(() => document.querySelectorAll("audio, video").forEach(addMedia), 2000);

  st.stop = () => new Promise((resolve) => {
    if (!recorder || recorder.state === "inactive") return resolve();
    recorder.addEventListener("stop", () => setTimeout(resolve, 500), { once: true });
    recorder.stop();
  });
})();
