import argparse
import os
import secrets
import sqlite3
import socket
import threading
import time
import uuid
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple
from urllib.parse import unquote

import cv2
from PIL import Image, ImageTk
import qrcode
import tkinter as tk
from tkinter import messagebox, ttk


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class ClipRecord:
    clip_id: str
    key_pressed: str
    camera_label: str
    file_path: str
    created_at: str
    duration_seconds: int
    token: str
    expires_at: str


class ClipStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._columns: set[str] = set()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS clips (
                    clip_id TEXT PRIMARY KEY,
                    key_pressed TEXT NOT NULL,
                    camera_label TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL
                )
                """
            )
            conn.commit()
            rows = conn.execute("PRAGMA table_info(clips)").fetchall()
            self._columns = {str(row["name"]) for row in rows}
            if "token" not in self._columns:
                conn.execute("ALTER TABLE clips ADD COLUMN token TEXT")
            if "expires_at" not in self._columns:
                conn.execute("ALTER TABLE clips ADD COLUMN expires_at TEXT")
            conn.commit()
            rows = conn.execute("PRAGMA table_info(clips)").fetchall()
            self._columns = {str(row["name"]) for row in rows}

    def add(self, record: ClipRecord) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO clips (
                    clip_id, key_pressed, camera_label, file_path,
                    created_at, duration_seconds, token, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.clip_id,
                    record.key_pressed,
                    record.camera_label,
                    record.file_path,
                    record.created_at,
                    record.duration_seconds,
                    record.token,
                    record.expires_at,
                ),
            )
            conn.commit()

    def list_recent(self, limit: int = 200) -> List[sqlite3.Row]:
        with self._lock, self._connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM clips
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def get_by_id(self, clip_id: str) -> Optional[sqlite3.Row]:
        with self._lock, self._connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM clips
                WHERE clip_id = ?
                """,
                (clip_id,),
            ).fetchone()

    def has_file_path(self, file_path: str) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM clips
                WHERE file_path = ?
                LIMIT 1
                """,
                (file_path,),
            ).fetchone()
            return row is not None

    def get_by_token(self, token: str) -> Optional[sqlite3.Row]:
        with self._lock, self._connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM clips
                WHERE token = ?
                """,
                (token,),
            ).fetchone()

    def ensure_share_token(self, clip_id: str, ttl_hours: int) -> str:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT clip_id, token
                FROM clips
                WHERE clip_id = ?
                """,
                (clip_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Clipe nao encontrado para gerar link.")

            token = row["token"] if row["token"] else secrets.token_urlsafe(24)
            expires_at = (utc_now() + timedelta(hours=ttl_hours)).isoformat()
            conn.execute(
                """
                UPDATE clips
                SET token = ?, expires_at = ?
                WHERE clip_id = ?
                """,
                (token, expires_at, clip_id),
            )
            conn.commit()
            return token

    def list_expired(self, older_than: datetime) -> List[sqlite3.Row]:
        with self._lock, self._connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM clips
                WHERE created_at < ?
                """,
                (older_than.isoformat(),),
            ).fetchall()

    def delete_clip(self, clip_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM clips WHERE clip_id = ?", (clip_id,))
            conn.commit()


class LocalShareServer:
    def __init__(self, store: ClipStore, host: str, port: int) -> None:
        self.store = store
        self.host = host
        self.port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def _build_handler(self):
        store = self.store

        class ShareHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if not self.path.startswith("/access/"):
                    self.send_error(HTTPStatus.NOT_FOUND, "Endpoint invalido.")
                    return

                token = unquote(self.path[len("/access/") :]).strip()
                if not token:
                    self.send_error(HTTPStatus.BAD_REQUEST, "Token ausente.")
                    return

                row = store.get_by_token(token)
                if row is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "Token invalido.")
                    return

                expires_raw = row["expires_at"]
                if expires_raw:
                    expires_at = datetime.fromisoformat(expires_raw)
                    if utc_now() > expires_at:
                        self.send_error(HTTPStatus.FORBIDDEN, "Token expirado.")
                        return

                clip_path = Path(row["file_path"])
                if not clip_path.exists():
                    self.send_error(HTTPStatus.NOT_FOUND, "Arquivo nao encontrado.")
                    return

                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(clip_path.stat().st_size))
                self.send_header("Content-Disposition", f'attachment; filename="{clip_path.name}"')
                self.end_headers()
                with clip_path.open("rb") as file_obj:
                    self.wfile.write(file_obj.read())

            def log_message(self, format: str, *args) -> None:  # noqa: A003
                return

        return ShareHandler

    def start(self) -> None:
        handler = self._build_handler()
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()


class ReplayGUI:
    def __init__(
        self,
        root: tk.Tk,
        data_dir: Path,
        key_map: Dict[str, str],
        camera_index: int,
        clip_seconds: int,
        buffer_seconds: int,
        retention_days: int,
        link_ttl_hours: int,
        share_host: str,
        share_port: int,
    ) -> None:
        self.root = root
        self.data_dir = data_dir
        self.clips_dir = data_dir / "clips"
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.store = ClipStore(data_dir / "replay.db")

        self.key_map = key_map
        self.camera_index = camera_index
        self.clip_seconds = clip_seconds
        self.buffer_seconds = buffer_seconds
        self.retention_days = retention_days
        self.link_ttl_hours = link_ttl_hours
        self.share_host = share_host
        self.share_port = share_port
        self.share_base_url = f"http://{self.share_host}:{self.share_port}"

        self.buffer: Deque[Tuple[float, any]] = deque()
        self.buffer_lock = threading.Lock()
        self.save_lock = threading.Lock()
        self.is_saving = False

        self.capture = cv2.VideoCapture(self.camera_index)
        if not self.capture.isOpened():
            raise RuntimeError(f"Nao foi possivel abrir a camera {self.camera_index}.")

        fps = self.capture.get(cv2.CAP_PROP_FPS)
        self.camera_fps = fps if fps and fps > 1 else 30.0

        self.video_photo: Optional[ImageTk.PhotoImage] = None
        self.qr_photo: Optional[ImageTk.PhotoImage] = None
        self.current_frame_shape: Optional[Tuple[int, int]] = None
        self.running = True

        self.share_server = LocalShareServer(self.store, self.share_host, self.share_port)
        self.share_enabled = True
        try:
            self.share_server.start()
        except OSError as exc:
            self.share_enabled = False
            messagebox.showwarning(
                "Aviso",
                f"Nao foi possivel iniciar servidor local de compartilhamento na porta {self.share_port}: {exc}",
            )

        self.status_var = tk.StringVar(value="Sistema pronto. Clique na janela e pressione as teclas configuradas.")
        self._build_ui()
        self._bind_events()
        self._refresh_clip_table()
        self._schedule_cleanup()
        self._schedule_frame_loop()

    def _build_ui(self) -> None:
        self.root.title("Replay 15s - Webcam + Teclado")
        self.root.geometry("980x700")
        self.root.minsize(920, 620)

        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        top = ttk.Frame(main)
        top.pack(fill="x")

        ttk.Label(top, text="Preview da webcam", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        key_desc = ", ".join([f"{k} -> {v}" for k, v in self.key_map.items()])
        ttk.Label(top, text=f"Atalhos ativos: {key_desc}").pack(anchor="w", pady=(2, 6))

        self.video_label = ttk.Label(main, text="Carregando camera...", anchor="center")
        self.video_label.pack(fill="both", expand=True)

        info = ttk.Frame(main)
        info.pack(fill="x", pady=(8, 0))
        ttk.Label(info, textvariable=self.status_var).pack(side="left", anchor="w")

        actions = ttk.Frame(main)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="Abrir pasta de clipes", command=self._open_clips_folder).pack(side="left")
        ttk.Button(actions, text="Atualizar lista", command=self._sync_and_refresh).pack(side="left", padx=8)
        ttk.Button(actions, text="Exportar QR do selecionado", command=self._export_selected_qr).pack(
            side="left", padx=8
        )

        table_frame = ttk.Frame(main)
        table_frame.pack(fill="both", expand=False, pady=(10, 0))

        columns = ("created_at", "key_pressed", "camera_label", "file_name")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", height=10)
        self.table.heading("created_at", text="Data/Hora")
        self.table.heading("key_pressed", text="Tecla")
        self.table.heading("camera_label", text="Camera")
        self.table.heading("file_name", text="Arquivo")
        self.table.column("created_at", width=220)
        self.table.column("key_pressed", width=60, anchor="center")
        self.table.column("camera_label", width=180)
        self.table.column("file_name", width=460)
        self.table.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        scrollbar.pack(side="right", fill="y")
        self.table.configure(yscrollcommand=scrollbar.set)

        ttk.Label(
            main,
            text="Dica: de duplo clique em um item para abrir o arquivo do clipe.",
        ).pack(anchor="w", pady=(8, 0))

    def _bind_events(self) -> None:
        self.root.bind("<KeyPress>", self._on_key_press)
        self.table.bind("<Double-1>", self._on_double_click)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _schedule_frame_loop(self) -> None:
        self._frame_loop()
        self.root.after(30, self._schedule_frame_loop)

    def _frame_loop(self) -> None:
        if not self.running:
            return
        ok, frame = self.capture.read()
        if not ok:
            self.status_var.set("Falha ao ler frame da webcam.")
            return

        self.current_frame_shape = (frame.shape[1], frame.shape[0])
        now_ts = time.time()
        with self.buffer_lock:
            self.buffer.append((now_ts, frame.copy()))
            self._prune_buffer(now_ts)

        self._render_frame(frame)

    def _prune_buffer(self, now_ts: float) -> None:
        min_ts = now_ts - self.buffer_seconds
        while self.buffer and self.buffer[0][0] < min_ts:
            self.buffer.popleft()

    def _render_frame(self, frame: any) -> None:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)
        image = image.resize((920, 500), Image.Resampling.LANCZOS)
        self.video_photo = ImageTk.PhotoImage(image=image)
        self.video_label.configure(image=self.video_photo, text="")

    def _on_key_press(self, event: tk.Event) -> None:
        key = event.char
        if not key or key not in self.key_map:
            return
        if self.is_saving:
            self.status_var.set("Aguarde: ainda estou salvando o ultimo clipe.")
            return
        self.is_saving = True
        self.status_var.set(f"Tecla '{key}' detectada. Salvando replay...")
        camera_label = self.key_map[key]
        worker = threading.Thread(
            target=self._save_clip_worker,
            args=(key, camera_label),
            daemon=True,
        )
        worker.start()

    def _save_clip_worker(self, key: str, camera_label: str) -> None:
        try:
            with self.save_lock:
                frames = self._last_seconds_frames(self.clip_seconds)
                if len(frames) < 2:
                    self.root.after(
                        0,
                        lambda: self.status_var.set("Buffer insuficiente. Aguarde alguns segundos e tente novamente."),
                    )
                    return

                now = utc_now()
                clip_id = str(uuid.uuid4())
                file_name = (
                    f"{now.strftime('%Y%m%d_%H%M%S')}_{camera_label.replace(' ', '_').lower()}_{clip_id[:8]}.mp4"
                )
                output_path = self.clips_dir / file_name

                self._write_mp4(frames, output_path)
                record = ClipRecord(
                    clip_id=clip_id,
                    key_pressed=key,
                    camera_label=camera_label,
                    file_path=str(output_path),
                    created_at=now.isoformat(),
                    duration_seconds=self.clip_seconds,
                    token=secrets.token_urlsafe(24),
                    expires_at=(now + timedelta(hours=self.link_ttl_hours)).isoformat(),
                )
                self.store.add(record)
                self.root.after(0, self._sync_and_refresh)
                self.root.after(
                    0,
                    lambda: self.status_var.set(f"Clique salvo com sucesso: {output_path.name}"),
                )
        except Exception as exc:
            # Captura a mensagem no momento do except, evitando perda de escopo no callback do Tkinter.
            error_message = str(exc)
            self.root.after(
                0,
                lambda msg=error_message: self.status_var.set(f"Erro ao salvar clipe: {msg}"),
            )
        finally:
            self.is_saving = False

    def _last_seconds_frames(self, seconds: int) -> List[any]:
        cutoff = time.time() - seconds
        with self.buffer_lock:
            return [frame for ts, frame in self.buffer if ts >= cutoff]

    def _write_mp4(self, frames: List[any], output_path: Path) -> None:
        height, width = frames[0].shape[:2]
        # Garante duracao de reproducao consistente com clip_seconds.
        output_fps = max(len(frames) / float(self.clip_seconds), 1.0)
        writer = cv2.VideoWriter(
            str(output_path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            output_fps,
            (width, height),
        )
        if not writer.isOpened():
            raise RuntimeError("Falha ao abrir VideoWriter para salvar MP4.")
        for frame in frames:
            writer.write(frame)
        writer.release()

    def _refresh_clip_table(self) -> None:
        for iid in self.table.get_children():
            self.table.delete(iid)
        for row in self.store.list_recent(limit=300):
            file_name = Path(row["file_path"]).name
            self.table.insert(
                "",
                "end",
                iid=row["clip_id"],
                values=(
                    row["created_at"],
                    row["key_pressed"],
                    row["camera_label"],
                    file_name,
                ),
            )

    def _sync_and_refresh(self) -> None:
        imported = self._sync_clips_folder_to_db()
        self._refresh_clip_table()
        if imported > 0:
            self.status_var.set(f"Sincronizacao concluida: {imported} clipe(s) importado(s) da pasta.")

    def _sync_clips_folder_to_db(self) -> int:
        imported = 0
        for clip_path in sorted(self.clips_dir.glob("*.mp4")):
            clip_path_abs = str(clip_path.resolve())
            if self.store.has_file_path(clip_path_abs):
                continue

            created_at = datetime.fromtimestamp(clip_path.stat().st_mtime, tz=UTC).isoformat()
            camera_label = "camera_importada"
            key_pressed = "?"

            stem = clip_path.stem
            parts = stem.split("_")
            if len(parts) >= 3:
                maybe_camera = f"{parts[-3]}_{parts[-2]}" if parts[-3] == "camera" else parts[-2]
                if maybe_camera.startswith("camera_"):
                    camera_label = maybe_camera
                    key_pressed = maybe_camera.replace("camera_", "")

            self.store.add(
                ClipRecord(
                    clip_id=str(uuid.uuid4()),
                    key_pressed=key_pressed,
                    camera_label=camera_label,
                    file_path=clip_path_abs,
                    created_at=created_at,
                    duration_seconds=self.clip_seconds,
                    token=secrets.token_urlsafe(24),
                    expires_at=(utc_now() + timedelta(hours=self.link_ttl_hours)).isoformat(),
                )
            )
            imported += 1
        return imported

    def _export_selected_qr(self) -> None:
        if not self.share_enabled:
            messagebox.showerror("Erro", "Servidor de compartilhamento local nao esta ativo.")
            return

        item_id = self.table.focus()
        if not item_id:
            messagebox.showinfo("Selecionar clipe", "Selecione um clipe na lista para gerar o QR Code.")
            return

        row = self.store.get_by_id(item_id)
        if row is None:
            messagebox.showerror("Erro", "Clipe selecionado nao encontrado no banco.")
            return

        token = self.store.ensure_share_token(item_id, self.link_ttl_hours)
        share_url = f"{self.share_base_url}/access/{token}"
        self._show_qr_window(share_url)
        self.status_var.set("QR Code gerado. Compartilhe com quem estiver no mesmo Wi-Fi.")

    def _show_qr_window(self, share_url: str) -> None:
        qr = qrcode.QRCode(box_size=8, border=2)
        qr.add_data(share_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

        top = tk.Toplevel(self.root)
        top.title("QR Code de compartilhamento")
        top.geometry("420x520")

        ttk.Label(
            top,
            text="Escaneie este QR Code no celular\n(conectado ao mesmo Wi-Fi)",
            justify="center",
        ).pack(pady=(12, 8))

        self.qr_photo = ImageTk.PhotoImage(qr_img)
        qr_label = ttk.Label(top, image=self.qr_photo)
        qr_label.pack(pady=(0, 12))

        link_box = tk.Text(top, height=3, wrap="word")
        link_box.insert("1.0", share_url)
        link_box.configure(state="disabled")
        link_box.pack(fill="x", padx=12)

        def copy_link() -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(share_url)
            self.status_var.set("Link de compartilhamento copiado.")

        ttk.Button(top, text="Copiar link", command=copy_link).pack(pady=12)

    def _on_double_click(self, event: tk.Event) -> None:
        item_id = self.table.focus()
        if not item_id:
            return
        target = self.store.get_by_id(item_id)
        if target is None:
            return
        clip_path = Path(target["file_path"])
        if not clip_path.exists():
            messagebox.showerror("Erro", "Arquivo do clipe nao encontrado no disco.")
            return
        os.startfile(str(clip_path))

    def _open_clips_folder(self) -> None:
        os.startfile(str(self.clips_dir))

    def _schedule_cleanup(self) -> None:
        self._run_cleanup()
        self.root.after(60 * 60 * 1000, self._schedule_cleanup)

    def _run_cleanup(self) -> None:
        cutoff = utc_now() - timedelta(days=self.retention_days)
        expired_rows = self.store.list_expired(cutoff)
        removed = 0
        for row in expired_rows:
            file_path = Path(row["file_path"])
            if file_path.exists():
                try:
                    file_path.unlink()
                except OSError:
                    continue
            self.store.delete_clip(row["clip_id"])
            removed += 1
        if removed > 0:
            self.status_var.set(f"Limpeza automatica removeu {removed} clipe(s) expirado(s).")
            self._refresh_clip_table()

    def _on_close(self) -> None:
        self.running = False
        self.share_server.stop()
        if self.capture:
            self.capture.release()
        self.root.destroy()


def parse_key_map(raw_keys: str) -> Dict[str, str]:
    keys = [key.strip() for key in raw_keys.split(",") if key.strip()]
    mapping: Dict[str, str] = {}
    for key in keys:
        mapping[key] = f"camera_{key}"
    return mapping


def discover_local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        return ip
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay local com interface grafica, webcam e atalhos por teclado.")
    parser.add_argument("--camera-index", type=int, default=0)
    parser.add_argument("--clip-seconds", type=int, default=15)
    parser.add_argument("--buffer-seconds", type=int, default=30)
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--link-ttl-hours", type=int, default=12)
    parser.add_argument("--share-port", type=int, default=8765)
    parser.add_argument("--keys", default="1,2,3", help="Teclas ativas (ex: 1,2,3,4).")
    parser.add_argument(
        "--data-dir",
        default=str(Path(__file__).resolve().parent / "data"),
        help="Diretorio para banco e clipes.",
    )
    args = parser.parse_args()

    key_map = parse_key_map(args.keys)
    if not key_map:
        raise ValueError("Informe ao menos uma tecla valida em --keys.")

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    share_host = discover_local_ip()

    root = tk.Tk()
    app = ReplayGUI(
        root=root,
        data_dir=data_dir,
        key_map=key_map,
        camera_index=args.camera_index,
        clip_seconds=args.clip_seconds,
        buffer_seconds=args.buffer_seconds,
        retention_days=args.retention_days,
        link_ttl_hours=args.link_ttl_hours,
        share_host=share_host,
        share_port=args.share_port,
    )
    app._sync_and_refresh()
    root.mainloop()


if __name__ == "__main__":
    main()
