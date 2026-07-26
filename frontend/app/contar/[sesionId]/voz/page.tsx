"use client";

import { useState, useEffect, useRef } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { api, ApiError } from "@/lib/api";
import { Logo } from "@/components/Logo";
import { MicIcon, KeyboardIcon, SearchIcon, StopIcon, KMarkIcon } from "@/components/Icons";

/**
 * K mark de Colsubsidio.
 * Usa el archivo /public/k-mark.png (auto-centrado).
 * Si no existe, usa logo.png y muestra la K con CSS.
 */
function KMark({ size = 140 }: { size?: number }) {
  return (
    <div
      style={{
        width: size,
        height: size,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        paddingRight: size * 0.05,  // very slight shift to the right
      }}
    >
      <img
        src="/k-mark.png?v=7"
        alt="Colsubsidio"
        draggable={false}
        style={{
          maxWidth: "85%",
          maxHeight: "85%",
          objectFit: "contain",
          display: "block",
        }}
        onError={(e) => {
          const target = e.currentTarget as HTMLImageElement;
          target.src = "/logo.png?v=7";
          target.style.maxWidth = "100%";
          target.style.maxHeight = "100%";
          target.style.objectFit = "cover";
          target.style.objectPosition = "left center";
          target.style.clipPath = "inset(0 70% 0 0)";
        }}
      />
    </div>
  );
}

// ─── Tipos ───────────────────────────────────────────────────────────────

type VoiceState =
  | "idle"
  | "recording"
  | "processing"
  | "confirm"
  | "alerta"
  | "responding"
  | "error";

interface ParsedProducto {
  nombre: string;
  cantidad: number;
  unidad: string;
}

interface AlertaSospechosa {
  pendingId: number;
  producto: string;
  cantidad: number;
  unidad: string;
  residual: number;
  umbral: number;
}

interface SesionEstado {
  sesion_id: number;
  bodega_id: number;
}

interface ToolCall {
  name: string;
  arguments: Record<string, any>;
}

interface PendingItem {
  pending_id: number;
  tool_name: string;
  arguments: Record<string, any>;
}

interface RegistrarVozFastPath {
  via: "regex_fastpath";
  pending_id: number;
  producto: string;
  cantidad: number;
  unidad: string;
}

interface RegistrarVozLLM {
  via: "llm";
  tool_calls: ToolCall[];
  pending: PendingItem[];
  raw_output?: string;
}

interface PendingRow {
  status: string;
  decision?: string;
  residual?: number;
  umbral?: number;
  payload?: any;
}

// ─── Constantes ──────────────────────────────────────────────────────────
//
// Rutas relativas: las resuelve el mismo origen (dev o el dominio publico
// detras de Cloudflare) via el proxy de app/api/[...path]/route.ts hacia
// el api-gateway.

const API_KEY = process.env.NEXT_PUBLIC_API_KEY || "";

function authHeaders(): Record<string, string> {
  return API_KEY ? { "X-API-Key": API_KEY } : {};
}

// ─── Página ──────────────────────────────────────────────────────────────

export default function VozPage() {
  const params = useParams();
  const router = useRouter();
  const sesionId = Number(params.sesionId);

  const [state, setState] = useState<VoiceState>("idle");
  const [errorMsg, setErrorMsg] = useState<string>("");

  const [transcribedText, setTranscribedText] = useState<string>("");
  const [parsedProducto, setParsedProducto] = useState<ParsedProducto | null>(null);
  const [narratorText, setNarratorText] = useState<string>("");
  const [alerta, setAlerta] = useState<AlertaSospechosa | null>(null);

  const bodegaIdRef = useRef<number | null>(null);

  // Refs de audio
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const animFrameRef = useRef<number>(0);
  const audioPlayerRef = useRef<HTMLAudioElement | null>(null);
  const pendingIdRef = useRef<number | null>(null);

  // Volume reactivo (0..1) — actualiza via rAF
  const [volume, setVolume] = useState(0);

  // ── Bodega de la sesión (para consultas/alertas por voz) ──────────────
  useEffect(() => {
    api<SesionEstado>(`/api/sesion/${sesionId}/estado`)
      .then((s) => {
        bodegaIdRef.current = s.bodega_id;
      })
      .catch((e) => console.error("Error cargando sesión:", e));
  }, [sesionId]);

  // ── Cleanup al desmontar ──────────────────────────────────────────────
  useEffect(() => {
    return () => {
      stopAllAudio();
    };
  }, []);

  function stopAllAudio() {
    if (animFrameRef.current) cancelAnimationFrame(animFrameRef.current);
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    }
    if (audioContextRef.current && audioContextRef.current.state !== "closed") {
      audioContextRef.current.close().catch(() => {});
      audioContextRef.current = null;
    }
    if (audioPlayerRef.current) {
      audioPlayerRef.current.pause();
      audioPlayerRef.current.src = "";
    }
  }

  // ── Empezar grabación ─────────────────────────────────────────────────
  async function startRecording() {
    setErrorMsg("");
    setTranscribedText("");
    setParsedProducto(null);
    setNarratorText("");
    setAlerta(null);

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      const AC = window.AudioContext || (window as any).webkitAudioContext;
      const audioContext = new AC();
      audioContextRef.current = audioContext;

      const source = audioContext.createMediaStreamSource(stream);
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      analyserRef.current = analyser;

      const mediaRecorder = new MediaRecorder(stream, {
        mimeType: pickMimeType(),
      });
      mediaRecorderRef.current = mediaRecorder;
      chunksRef.current = [];

      mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };

      mediaRecorder.onstop = async () => {
        const blob = new Blob(chunksRef.current, { type: "audio/webm" });
        stopAllAudio();
        await transcribirYProcesar(blob);
      };

      mediaRecorder.start();
      setState("recording");
      medirVolumen();
    } catch (e: any) {
      console.error("Error accediendo al micro:", e);
      setErrorMsg(
        e?.name === "NotAllowedError"
          ? "Necesitamos permiso del micrófono para usar el modo voz."
          : "No se pudo acceder al micrófono."
      );
      setState("error");
    }
  }

  function pickMimeType(): string {
    const candidates = [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/mp4",
      "audio/ogg;codecs=opus",
    ];
    for (const c of candidates) {
      if (typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(c)) {
        return c;
      }
    }
    return "audio/webm";
  }

  function medirVolumen() {
    if (!analyserRef.current) return;
    const buf = new Uint8Array(analyserRef.current.fftSize);
    const tick = () => {
      if (!analyserRef.current) return;
      analyserRef.current.getByteTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = (buf[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / buf.length);
      setVolume(Math.min(1, rms * 3.5));
      animFrameRef.current = requestAnimationFrame(tick);
    };
    tick();
  }

  // ── Parar grabación ───────────────────────────────────────────────────
  function stopRecording() {
    if (
      mediaRecorderRef.current &&
      mediaRecorderRef.current.state === "recording"
    ) {
      mediaRecorderRef.current.stop();
      setState("processing");
    }
  }

  function cancelarGrabacion() {
    if (mediaRecorderRef.current) {
      mediaRecorderRef.current.ondataavailable = null;
      mediaRecorderRef.current.onstop = null;
      if (mediaRecorderRef.current.state === "recording") {
        mediaRecorderRef.current.stop();
      }
    }
    stopAllAudio();
    setState("idle");
  }

  // ── Helpers compartidos: narrar + hablar ──────────────────────────────
  //
  //  /api/audio/speak devuelve PCM crudo (content-type "audio/pcm", sin
  //  ningun header RIFF/WAV) tanto desde Kokoro como desde ElevenLabs (esta
  //  configurado con tts_output=pcm_24000). Un <audio> del navegador NO
  //  puede reproducir PCM crudo directamente -- necesita un contenedor con
  //  metadata (sample rate, canales, bits). Por eso el audio nunca sonaba
  //  aunque el fetch daba 200 OK: hacia falta envolver el PCM en un header
  //  WAV de 44 bytes antes de armar el Blob.
  function pcmToWavBlob(
    pcm: ArrayBuffer,
    sampleRate: number,
    numChannels: number,
    bitsPerSample: number
  ): Blob {
    const bytesPerSample = bitsPerSample / 8;
    const blockAlign = numChannels * bytesPerSample;
    const byteRate = sampleRate * blockAlign;
    const dataSize = pcm.byteLength;

    const buffer = new ArrayBuffer(44 + dataSize);
    const view = new DataView(buffer);
    const writeStr = (offset: number, str: string) => {
      for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
    };

    writeStr(0, "RIFF");
    view.setUint32(4, 36 + dataSize, true);
    writeStr(8, "WAVE");
    writeStr(12, "fmt ");
    view.setUint32(16, 16, true); // tamaño del subchunk fmt (PCM)
    view.setUint16(20, 1, true); // formato PCM entero
    view.setUint16(22, numChannels, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, byteRate, true);
    view.setUint16(32, blockAlign, true);
    view.setUint16(34, bitsPerSample, true);
    writeStr(36, "data");
    view.setUint32(40, dataSize, true);

    new Uint8Array(buffer, 44).set(new Uint8Array(pcm));
    return new Blob([buffer], { type: "audio/wav" });
  }

  async function hablar(texto: string) {
    if (!texto) return;
    try {
      const ttsRes = await fetch(`/api/audio/speak`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ text: texto }),
      });
      if (!ttsRes.ok) {
        console.warn("TTS falló, pero seguimos con texto");
        return;
      }

      const contentType = ttsRes.headers.get("content-type") || "";
      const raw = await ttsRes.arrayBuffer();
      let audioBlob: Blob;
      if (contentType.includes("pcm")) {
        const sampleRate = Number(ttsRes.headers.get("x-sample-rate") || "24000");
        const channels = Number(ttsRes.headers.get("x-channels") || "1");
        const sampleWidthBytes = Number(ttsRes.headers.get("x-sample-width") || "2");
        audioBlob = pcmToWavBlob(raw, sampleRate, channels, sampleWidthBytes * 8);
      } else {
        audioBlob = new Blob([raw], { type: contentType || "audio/mpeg" });
      }

      const url = URL.createObjectURL(audioBlob);
      if (audioPlayerRef.current) {
        audioPlayerRef.current.src = url;
        audioPlayerRef.current.onended = () => URL.revokeObjectURL(url);
        await audioPlayerRef.current.play().catch((e) => {
          console.warn("No se pudo reproducir el audio:", e);
        });
      }
    } catch (e) {
      console.warn("Error de TTS:", e);
    }
  }

  async function mostrarYHablar(texto: string) {
    setNarratorText(texto);
    setState("responding");
    await hablar(texto);
  }

  async function pollPending(id: number): Promise<PendingRow> {
    const start = Date.now();
    while (true) {
      const data = await api<PendingRow>(`/api/pending/${id}`);
      if (data.status !== "PENDING") return data;
      if (Date.now() - start > 15000) throw new Error("Timeout esperando Kalman");
      await new Promise((r) => setTimeout(r, 200));
    }
  }

  // ── Lecturas: consultar_inventario / investigar_sospechosos ───────────
  async function resolverConsulta(producto?: string) {
    try {
      const qs = new URLSearchParams();
      if (producto) qs.set("producto", producto);
      if (bodegaIdRef.current != null) qs.set("bodega_id", String(bodegaIdRef.current));
      let inv: any;
      try {
        inv = await api(`/inventory?${qs.toString()}`);
      } catch (e) {
        await mostrarYHablar(
          producto ? `No encontré "${producto}" en el inventario.` : "No pude consultar el inventario."
        );
        return;
      }
      const data = Array.isArray(inv)
        ? inv.length === 1
          ? {
              producto: inv[0].nombre,
              stock_actual: inv[0].stock_actual,
              unidad: inv[0].unidad,
              bodega: inv[0].bodega || "la bodega",
            }
          : {
              producto: "varios",
              stock_actual: inv.reduce((s: number, r: any) => s + Number(r.stock_actual || 0), 0),
              unidad: "unidades",
              bodega: `${inv.length} bodegas`,
            }
        : {
            producto: inv.nombre,
            stock_actual: inv.stock_actual,
            unidad: inv.unidad,
            bodega: inv.bodega || "la bodega",
          };
      const narrate = await api<{ text: string }>("/api/narrate", {
        method: "POST",
        body: JSON.stringify({ event: "consulta", data }),
      });
      await mostrarYHablar(narrate.text);
    } catch (e: any) {
      console.error("Error consultando inventario:", e);
      setErrorMsg(e?.message || "Error consultando el inventario.");
      setState("error");
    }
  }

  async function resolverSospechosos(producto?: string) {
    try {
      const qs = producto ? `?${new URLSearchParams({ producto }).toString()}` : "";
      const rows = await api<any[]>(`/sospechosos${qs}`);
      const data = !rows.length
        ? { total: 0 }
        : (() => {
            const top = rows.reduce((a, b) => (b.puntaje_riesgo > a.puntaje_riesgo ? b : a));
            return {
              total: rows.length,
              top_producto: top.producto_nombre,
              top_cantidad: top.cantidad_reportada,
              top_puntaje: top.puntaje_riesgo,
              top_tipo: top.tipo,
              top_unidad: "Unidad",
            };
          })();
      const narrate = await api<{ text: string }>("/api/narrate", {
        method: "POST",
        body: JSON.stringify({ event: "sospechosos", data }),
      });
      await mostrarYHablar(narrate.text);
    } catch (e: any) {
      console.error("Error consultando sospechosos:", e);
      setErrorMsg(e?.message || "Error consultando sospechosos.");
      setState("error");
    }
  }

  // ── Transcribir + enviar al backend ───────────────────────────────────
  async function transcribirYProcesar(blob: Blob) {
    setState("processing");

    try {
      // 1) STT
      const fd = new FormData();
      fd.append("file", blob, "grabacion.webm");
      fd.append("language_code", "es");

      const sttRes = await fetch(`/api/audio/transcribir`, {
        method: "POST",
        body: fd,
        headers: authHeaders(),
      });

      if (!sttRes.ok) {
        const t = await sttRes.text();
        throw new Error(`STT ${sttRes.status}: ${t.slice(0, 200)}`);
      }
      const stt = await sttRes.json();
      const texto = (stt.text || "").trim();
      setTranscribedText(texto);

      if (!texto) {
        setErrorMsg("No te escuché. Probá de nuevo.");
        setState("error");
        return;
      }

      // 2) Pasar al endpoint de registro por voz — puede resolver rapido
      // (regex) o pasar por el LLM general, que tambien sabe consultar
      // inventario / investigar sospechosos, no solo registrar.
      const resp = await api<RegistrarVozFastPath | RegistrarVozLLM>(
        "/api/sesion/registrar-voz",
        {
          method: "POST",
          body: JSON.stringify({ sesion_id: sesionId, texto }),
        }
      );

      if (resp.via === "regex_fastpath") {
        setParsedProducto({
          nombre: resp.producto,
          cantidad: resp.cantidad,
          unidad: resp.unidad,
        });
        pendingIdRef.current = resp.pending_id;
        setState("confirm");
        return;
      }

      // via === "llm"
      const call = resp.tool_calls?.[0];

      if (!call) {
        // Sin tool call: charla general (b-link) u otra respuesta directa.
        if (resp.raw_output) {
          await mostrarYHablar(resp.raw_output);
        } else {
          setErrorMsg("No pude entender el pedido.");
          setState("error");
        }
        return;
      }

      if (call.name === "consultar_inventario") {
        await resolverConsulta(call.arguments?.producto);
        return;
      }
      if (call.name === "investigar_sospechosos") {
        await resolverSospechosos(call.arguments?.producto);
        return;
      }

      // Escritura (agregar_inventario / remover_inventario / registrar_conteo):
      // buscar el pending real encolado (las lecturas vienen con pending_id=0).
      const pend = (resp.pending || []).find((p) => p.pending_id > 0);
      if (!pend) {
        setErrorMsg("No pude registrar eso — revisá el producto o la cantidad.");
        setState("error");
        return;
      }
      pendingIdRef.current = pend.pending_id;
      const args = pend.arguments || {};
      const nombre = args.producto || args.nombre || "";
      if (nombre) {
        setParsedProducto({
          nombre,
          cantidad: args.cantidad,
          unidad: args.unidad || "",
        });
      }
      setState("confirm");
    } catch (e: any) {
      console.error("Error procesando voz:", e);
      setErrorMsg(e?.message || "Error procesando el audio.");
      setState("error");
    }
  }

  // ── Confirmar el parseo → esperar Kalman → narrar + hablar ────────────
  async function handleConfirm() {
    setState("processing");

    try {
      if (pendingIdRef.current != null) {
        const result = await pollPending(pendingIdRef.current);

        if (result.status === "RECHAZADA") {
          setErrorMsg("Rechazado por Kalman.");
          setState("error");
          return;
        }

        if (result.status === "SOSPECHOSA") {
          setAlerta({
            pendingId: pendingIdRef.current,
            producto: parsedProducto?.nombre || "",
            cantidad: parsedProducto?.cantidad ?? 0,
            unidad: parsedProducto?.unidad || "",
            residual: result.residual ?? 0,
            umbral: result.umbral ?? 0,
          });
          setState("alerta");
          return;
        }
        // ACEPTADA (u otro estado terminal) → sigue abajo
      }

      // Narrador
      const narrate = await api<{ text: string; backend: string }>("/api/narrate", {
        method: "POST",
        body: JSON.stringify({
          event: "aceptada",
          data: {
            producto: parsedProducto?.nombre,
            cantidad: parsedProducto?.cantidad,
            unidad: parsedProducto?.unidad,
          },
        }),
      });
      await mostrarYHablar(narrate.text);
    } catch (e: any) {
      console.error("Error confirmando:", e);
      setErrorMsg(e?.message || "Error confirmando el registro.");
      setState("error");
    }
  }

  // ── Resolver alerta SOSPECHOSA (Sí/No) — mismo mecanismo que la CLI ───
  async function resolverAlerta(confirmar: boolean) {
    if (!alerta) return;
    setState("processing");

    try {
      const puntaje = alerta.umbral ? Math.abs(alerta.residual) / alerta.umbral : 0;
      const resp = await api<{ pending: PendingItem[] }>("/query", {
        method: "POST",
        body: JSON.stringify({
          text: confirmar ? "si" : "no",
          session_id: String(sesionId),
          bodega_id: bodegaIdRef.current,
          pending_alert: {
            pending_id: alerta.pendingId,
            producto: alerta.producto,
            cantidad: alerta.cantidad,
            tipo: null,
            residual: alerta.residual,
            puntaje_riesgo: puntaje,
          },
        }),
      });

      const confirmPending = resp.pending?.find((p) => p.tool_name === "confirmar_movimiento");
      if (!confirmPending) {
        setErrorMsg("No se pudo procesar la confirmación.");
        setState("error");
        return;
      }
      await pollPending(confirmPending.pending_id);

      const narrate = await api<{ text: string }>("/api/narrate", {
        method: "POST",
        body: JSON.stringify({ event: confirmar ? "confirmada" : "rechazada", data: {} }),
      });
      setAlerta(null);
      await mostrarYHablar(narrate.text);
    } catch (e: any) {
      console.error("Error resolviendo alerta:", e);
      setErrorMsg(e?.message || "Error confirmando la alerta.");
      setState("error");
    }
  }

  // ── Reset para un nuevo pedido ────────────────────────────────────────
  function resetToIdle() {
    setState("idle");
    setErrorMsg("");
    setTranscribedText("");
    setParsedProducto(null);
    setNarratorText("");
    setAlerta(null);
    pendingIdRef.current = null;
  }

  // ── Render ────────────────────────────────────────────────────────────
  return (
    <div className="min-h-screen flex flex-col bg-white">
      <VozHeader sesionId={sesionId} onClose={() => router.push(`/contar/${sesionId}`)} />
      <div className="max-w-2xl mx-auto w-full flex border-b border-gray-200 bg-white">
        <Link
          href={`/contar/${sesionId}/voz`}
          className="flex-1 py-3 text-center font-semibold border-b-2 text-bAzul border-bAzul flex items-center justify-center gap-1.5"
        >
          <MicIcon size={22} />Voz
        </Link>
        <Link
          href={`/contar/${sesionId}`}
          className="flex-1 py-3 text-center font-semibold border-b-2 text-gray-600 border-transparent hover:text-gray-900 flex items-center justify-center gap-1.5"
        >
          <KeyboardIcon size={22} />Manual
        </Link>
        <Link
          href={`/contar/${sesionId}/buscar`}
          className="flex-1 py-3 text-center font-semibold border-b-2 text-gray-600 border-transparent hover:text-gray-900 flex items-center justify-center gap-1.5"
        >
          <SearchIcon size={22} />Buscar
        </Link>
      </div>

      <main className="flex-1 flex flex-col items-center justify-center px-6 pb-12">
        <VoiceCircle
          state={state}
          volume={volume}
          onTap={state === "idle" ? startRecording : undefined}
          onTapRecording={state === "recording" ? stopRecording : undefined}
        />

        <div className="mt-8 min-h-[80px] flex flex-col items-center justify-start max-w-md w-full">
          <StatusText
            state={state}
            errorMsg={errorMsg}
            transcribedText={transcribedText}
            parsedProducto={parsedProducto}
            narratorText={narratorText}
            alerta={alerta}
          />

          {state === "recording" && (
            <button
              onClick={cancelarGrabacion}
              className="mt-6 text-bAzul font-semibold hover:underline"
            >
              Cancelar
            </button>
          )}

          {state === "confirm" && (
            <div className="flex gap-3 mt-4 w-full max-w-sm">
              <button
                onClick={resetToIdle}
                className="flex-1 py-3 border-2 border-gray-300 text-gray-700 font-semibold rounded-lg hover:bg-gray-50"
              >
                Corregir
              </button>
              <button
                onClick={handleConfirm}
                className="flex-1 py-3 bg-bAmarillo text-gray-900 font-bold rounded-lg hover:brightness-95"
              >
                Confirmar
              </button>
            </div>
          )}

          {state === "alerta" && (
            <div className="flex gap-3 mt-4 w-full max-w-sm">
              <button
                onClick={() => resolverAlerta(false)}
                className="flex-1 py-3 border-2 border-gray-300 text-gray-700 font-semibold rounded-lg hover:bg-gray-50"
              >
                No, cancelar
              </button>
              <button
                onClick={() => resolverAlerta(true)}
                className="flex-1 py-3 bg-bAmarillo text-gray-900 font-bold rounded-lg hover:brightness-95"
              >
                Sí, es correcto
              </button>
            </div>
          )}

          {state === "responding" && (
            <button
              onClick={resetToIdle}
              className="mt-6 px-8 py-3 bg-bAmarillo text-gray-900 font-bold rounded-lg hover:brightness-95"
            >
              Continuar
            </button>
          )}

          {state === "error" && (
            <button
              onClick={resetToIdle}
              className="mt-6 px-8 py-3 bg-bAzul text-white font-semibold rounded-lg hover:brightness-95"
            >
              Reintentar
            </button>
          )}
        </div>
      </main>

      <audio ref={audioPlayerRef} hidden />
    </div>
  );
}

// ─── Header ──────────────────────────────────────────────────────────────

function VozHeader({ sesionId, onClose }: { sesionId: number; onClose: () => void }) {
  return (
    <>
      <header className="bg-bAzul text-white px-4 py-3 flex items-center justify-between sticky top-0 z-20">
        <Link href="/" className="flex items-center">
          <Logo size="sm" href={null} />
        </Link>
        <Link href="/admin" className="text-white font-semibold hover:underline">
          Admin
        </Link>
      </header>
      <div className="max-w-2xl mx-auto w-full px-4 py-3 border-b border-gray-200 flex items-center gap-2">
        <button
          onClick={onClose}
          className="w-10 h-10 rounded-full bg-gray-100 flex items-center justify-center text-gray-700 text-xl shrink-0"
          aria-label="Volver"
        >
          ←
        </button>
        <h1 className="flex-1 text-center font-bold text-base text-gray-900">
          Sesión #{sesionId} — Bodega cocina
        </h1>
        <div className="w-10 h-10 shrink-0" />
      </div>
    </>
  );
}

// ─── Círculo animado central ─────────────────────────────────────────────

function VoiceCircle({
  state,
  volume,
  onTap,
  onTapRecording,
}: {
  state: VoiceState;
  volume: number;
  onTap?: () => void;
  onTapRecording?: () => void;
}) {
  // Tamaño base del círculo en px
  const size = 240;
  // En grabación: leve escala con el volumen
  const recordingScale = 1 + volume * 0.06;

  const handleClick = () => {
    if (state === "idle" && onTap) onTap();
    if (state === "recording" && onTapRecording) onTapRecording();
  };

  return (
    <div
      className="relative flex items-center justify-center"
      style={{ width: size + 80, height: size + 80 }}
    >
      {/* Anillos expansivos de grabación */}
      {state === "recording" && (
        <>
          <div
            className="absolute rounded-full border-4 border-bAzul pulse-ring"
            style={{ width: size, height: size }}
          />
          <div
            className="absolute rounded-full border-4 border-bAzul pulse-ring-2"
            style={{ width: size, height: size }}
          />
          <div
            className="absolute rounded-full border-4 border-bAzul pulse-ring-3"
            style={{ width: size, height: size }}
          />
        </>
      )}

      {/* Círculo principal */}
      <button
        onClick={handleClick}
        disabled={state !== "idle" && state !== "recording"}
        className={`relative rounded-full flex items-center justify-center transition-transform ${
          state === "alerta" ? "bg-yellow-500" : "bg-bAzul"
        } ${state === "idle" ? "gentle-pulse cursor-pointer" : ""} ${
          state === "recording" ? "cursor-pointer" : ""
        }`}
        style={{
          width: size,
          height: size,
          transform: state === "recording" ? `scale(${recordingScale})` : undefined,
        }}
        aria-label={state === "idle" ? "Toca para hablar" : "Detener grabación"}
      >
        <CircleContent state={state} />
      </button>
    </div>
  );
}

function CircleContent({ state }: { state: VoiceState }) {
  const iconColor = "#FFD000"; // bAmarillo

  if (state === "processing") {
    return (
      // Spokes girando (8 líneas)
      <svg
        className="spokes-rotate"
        width="120"
        height="120"
        viewBox="0 0 120 120"
        fill="none"
      >
        {Array.from({ length: 8 }).map((_, i) => {
          const angle = (i * 360) / 8;
          return (
            <rect
              key={i}
              x="56"
              y="20"
              width="8"
              height="32"
              rx="4"
              fill={iconColor}
              transform={`rotate(${angle} 60 60)`}
              opacity={0.3 + (i / 8) * 0.7}
            />
          );
        })}
      </svg>
    );
  }

  if (state === "confirm") {
    return (
      // Check grande
      <svg
        className="pop-in"
        width="140"
        height="140"
        viewBox="0 0 140 140"
        fill="none"
      >
        <path
          d="M35 75 L60 100 L105 45"
          stroke={iconColor}
          strokeWidth="16"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  }

  if (state === "alerta") {
    return (
      <span className="text-6xl" role="img" aria-label="Alerta">
        ⚠️
      </span>
    );
  }

  if (state === "responding") {
    return (
      // Tres puntos
      <div className="flex gap-3">
        <span
          className="dot-bounce-1 w-5 h-5 rounded-full"
          style={{ backgroundColor: iconColor }}
        />
        <span
          className="dot-bounce-2 w-5 h-5 rounded-full"
          style={{ backgroundColor: iconColor }}
        />
        <span
          className="dot-bounce-3 w-5 h-5 rounded-full"
          style={{ backgroundColor: iconColor }}
        />
      </div>
    );
  }

  if (state === "error") {
    return (
      <svg width="100" height="100" viewBox="0 0 100 100" fill="none">
        <path
          d="M50 15 L85 80 L15 80 Z"
          stroke={iconColor}
          strokeWidth="10"
          strokeLinejoin="round"
          fill="none"
        />
        <line x1="50" y1="38" x2="50" y2="60" stroke={iconColor} strokeWidth="10" strokeLinecap="round" />
        <circle cx="50" cy="70" r="5" fill={iconColor} />
      </svg>
    );
  }

  // IDLE & RECORDING → logo K de Colsubsidio (PNG)
  return <KMark size={140} />;
}

// ─── Texto de estado ─────────────────────────────────────────────────────

function StatusText({
  state,
  errorMsg,
  transcribedText,
  parsedProducto,
  narratorText,
  alerta,
}: {
  state: VoiceState;
  errorMsg: string;
  transcribedText: string;
  parsedProducto: ParsedProducto | null;
  narratorText: string;
  alerta: AlertaSospechosa | null;
}) {
  if (state === "idle") {
    return (
      <p className="text-gray-800 text-lg font-medium">Toca para hablar</p>
    );
  }
  if (state === "recording") {
    return <p className="text-gray-800 text-lg font-medium">Escuchando...</p>;
  }
  if (state === "processing") {
    return (
      <p className="text-gray-800 text-lg font-medium">Procesando tu pedido...</p>
    );
  }
  if (state === "confirm") {
    return (
      <div className="w-full text-left fade-up">
        <p className="text-gray-500 text-sm font-semibold uppercase tracking-wider mb-1">
          Entendí:
        </p>
        <p className="text-gray-900 text-lg italic mb-4">
          "{transcribedText}"
        </p>
        {parsedProducto && (
          <div className="border border-gray-300 rounded-lg p-4 bg-white">
            <p className="font-bold text-gray-900 text-lg uppercase">
              {parsedProducto.nombre}
            </p>
            <p className="text-gray-600 text-sm">
              Cantidad: {parsedProducto.cantidad} {parsedProducto.unidad}
            </p>
          </div>
        )}
      </div>
    );
  }
  if (state === "alerta" && alerta) {
    return (
      <div className="w-full text-left fade-up">
        <p className="text-yellow-700 text-sm font-semibold uppercase tracking-wider mb-1">
          ⚠ B-Link te está hablando — alerta de Kalman
        </p>
        <p className="text-gray-900 text-base mb-3">
          Vas a registrar{" "}
          <span className="font-bold tabular-nums">
            {alerta.cantidad} {alerta.unidad}
          </span>{" "}
          de <span className="font-bold uppercase">{alerta.producto}</span>. Es una
          diferencia mucho más grande de lo normal — ¿confirmás igual?
        </p>
      </div>
    );
  }
  if (state === "responding") {
    return (
      <p className="text-gray-800 text-lg text-center fade-up px-4">
        {narratorText}
      </p>
    );
  }
  if (state === "error") {
    return (
      <p className="text-red-600 text-center px-4">{errorMsg || "Algo salió mal."}</p>
    );
  }
  return null;
}
