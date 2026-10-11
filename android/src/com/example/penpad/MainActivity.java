package com.example.penpad;

import android.app.Activity;
import android.content.Context;
import android.graphics.Color;
import android.os.Bundle;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;

import java.io.BufferedOutputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

public class MainActivity extends Activity {

    static final String HOST = "127.0.0.1"; // via `adb reverse`; for Wi-Fi use the PC's LAN IP
    static final int PORT = 47300;

    // Protocol (must match penpad_server.py): <BBxxfffff, 24 bytes
    static final int HELLO = 0, HOVER = 1, DOWN = 2, MOVE = 3, UP = 4, LEAVE = 5, PING = 6;
    static final long PING_MS = 250; // keep-alive while idle; the PC drops a link that's silent for 2 s
    static final int BTN_BARREL = 0x01;
    static final int STYLUS_BUTTONS = MotionEvent.BUTTON_STYLUS_PRIMARY | MotionEvent.BUTTON_SECONDARY;

    static byte[] packet(int type, int buttons, float x, float y,
                         float pressure, float tilt, float orientation) {
        return ByteBuffer.allocate(24).order(ByteOrder.LITTLE_ENDIAN)
                .put((byte) type).put((byte) buttons).putShort((short) 0)
                .putFloat(x).putFloat(y).putFloat(pressure).putFloat(tilt).putFloat(orientation)
                .array();
    }

    /** Single TCP link to the PC, reconnects forever. Drops events while offline. */
    static final class Link {
        private static final LinkedBlockingQueue<byte[]> queue = new LinkedBlockingQueue<>(512);
        private static volatile byte[] hello;
        private static boolean started;

        static void setHello(byte[] p) { hello = p; send(p); }

        /** Non-blocking. If the PC has fallen behind and the queue is full, the backlog is stale:
         *  drop it rather than this packet, so a DOWN / UP / LEAVE is never the one lost. */
        static void send(byte[] p) {
            if (!queue.offer(p)) { queue.clear(); queue.offer(p); }
        }

        static synchronized void start() {
            if (started) return;
            started = true;
            Thread t = new Thread(Link::run, "penpad-link");
            t.setDaemon(true);
            t.start();
        }

        private static void run() {
            while (true) {
                try (Socket s = new Socket()) {
                    s.setTcpNoDelay(true);
                    s.connect(new InetSocketAddress(HOST, PORT), 2000);
                    queue.clear(); // stale strokes from while offline
                    OutputStream out = new BufferedOutputStream(s.getOutputStream(), 8192);
                    byte[] h = hello;
                    if (h != null) { out.write(h); out.flush(); }
                    byte[] ping = packet(PING, 0, 0f, 0f, 0f, 0f, 0f);
                    while (true) {
                        byte[] p = queue.poll(PING_MS, TimeUnit.MILLISECONDS);
                        out.write(p != null ? p : ping);
                        if (queue.isEmpty()) out.flush(); // batch bursts, flush when idle
                    }
                } catch (Exception e) {
                    try { Thread.sleep(1000); } catch (InterruptedException ie) { return; }
                }
            }
        }
    }

    static final class PadView extends View {
        PadView(Context ctx) {
            super(ctx);
            setBackgroundColor(Color.BLACK);
            setFocusable(true);
            setFocusableInTouchMode(true);
        }

        @Override
        protected void onAttachedToWindow() {
            super.onAttachedToWindow();
            requestFocus();
            // Deliver input as it arrives instead of batching to the display refresh (lower latency).
            requestUnbufferedDispatch(InputDevice.SOURCE_CLASS_POINTER);
        }

        @Override
        protected void onSizeChanged(int w, int h, int oldw, int oldh) {
            // pressure slot = screen density, so the PC can work in millimetres (tap dead zone etc.)
            float dpi = (getResources().getDisplayMetrics().xdpi + getResources().getDisplayMetrics().ydpi) / 2;
            Link.setHello(packet(HELLO, 0, w, h, dpi, 0f, 0f));
        }

        private static int penIndex(MotionEvent e) {
            for (int i = 0; i < e.getPointerCount(); i++) {
                int t = e.getToolType(i);
                if (t == MotionEvent.TOOL_TYPE_STYLUS || t == MotionEvent.TOOL_TYPE_ERASER) return i;
            }
            return -1;
        }

        private void emit(int type, MotionEvent e, int i) {
            int btn = (e.getButtonState() & STYLUS_BUTTONS) != 0 ? BTN_BARREL : 0;
            float w = getWidth(), h = getHeight();
            if (w <= 0 || h <= 0) return; // not laid out yet: positions would be NaN / infinite
            lastX = e.getX(i) / w;
            lastY = e.getY(i) / h;
            int histType = type == HOVER ? HOVER : MOVE;
            for (int k = 0; k < e.getHistorySize(); k++) { // batched samples keep fast strokes smooth
                Link.send(packet(histType, btn,
                        e.getHistoricalX(i, k) / w, e.getHistoricalY(i, k) / h,
                        e.getHistoricalPressure(i, k),
                        e.getHistoricalAxisValue(MotionEvent.AXIS_TILT, i, k),
                        e.getHistoricalOrientation(i, k)));
            }
            Link.send(packet(type, btn, e.getX(i) / w, e.getY(i) / h,
                    e.getPressure(i), e.getAxisValue(MotionEvent.AXIS_TILT, i), e.getOrientation(i)));
        }

        private boolean touching, hovering;
        private float lastX, lastY; // last pen position sent, normalized

        /** The app is losing the pen (paused, dialog or shade on top) possibly without the system
         *  sending CANCEL / HOVER_EXIT: end the stroke so the PC doesn't keep a button held. */
        void lift() {
            if (touching) Link.send(packet(UP, 0, lastX, lastY, 0f, 0f, 0f));
            else if (hovering) Link.send(packet(LEAVE, 0, lastX, lastY, 0f, 0f, 0f));
            touching = hovering = false;
        }

        @Override
        public boolean onTouchEvent(MotionEvent e) {
            int i = penIndex(e);
            if (i < 0) return true; // fingers / palm: swallow and ignore
            switch (e.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                case MotionEvent.ACTION_POINTER_DOWN:
                    if (e.getActionIndex() == i) { touching = true; hovering = false; emit(DOWN, e, i); }
                    break;
                case MotionEvent.ACTION_MOVE:
                    emit(MOVE, e, i);
                    break;
                case MotionEvent.ACTION_UP:
                case MotionEvent.ACTION_POINTER_UP:
                    if (e.getActionIndex() == i) { touching = false; emit(UP, e, i); }
                    break;
                case MotionEvent.ACTION_CANCEL:
                    touching = false;
                    emit(UP, e, i);
                    break;
            }
            return true;
        }

        /** Pen button pressed / released while hovering: report it right away instead of with the
         *  next hover move, so the PC's click / double-click / long-press timing is accurate. */
        @Override
        public boolean onGenericMotionEvent(MotionEvent e) {
            int a = e.getActionMasked();
            int i = penIndex(e);
            if (i >= 0 && !touching
                    && (a == MotionEvent.ACTION_BUTTON_PRESS || a == MotionEvent.ACTION_BUTTON_RELEASE)) {
                emit(HOVER, e, i);
                return true;
            }
            return super.onGenericMotionEvent(e);
        }

        @Override
        public boolean onHoverEvent(MotionEvent e) {
            int i = penIndex(e);
            if (i < 0) return true;
            switch (e.getActionMasked()) {
                case MotionEvent.ACTION_HOVER_ENTER:
                case MotionEvent.ACTION_HOVER_MOVE:
                    hovering = true;
                    emit(HOVER, e, i);
                    break;
                case MotionEvent.ACTION_HOVER_EXIT:
                    hovering = false;
                    emit(LEAVE, e, i);
                    break;
            }
            return true;
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Window w = getWindow();
        w.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        w.setDecorFitsSystemWindows(false);
        WindowManager.LayoutParams lp = w.getAttributes();
        lp.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_ALWAYS;
        w.setAttributes(lp);
        pad = new PadView(this);
        setContentView(pad);
        Link.start();
    }

    private PadView pad;

    @Override
    protected void onPause() {
        super.onPause();
        pad.lift();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (!hasFocus) {
            pad.lift();
            return;
        }
        WindowInsetsController c = getWindow().getInsetsController();
        if (c != null) {
            c.hide(WindowInsets.Type.systemBars());
            c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
        }
    }
}
