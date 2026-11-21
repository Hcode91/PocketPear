package com.hcode91.pocketpear

import android.app.Activity
import android.graphics.Bitmap
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.view.Gravity
import android.widget.Button
import android.widget.FrameLayout
import android.widget.Toast
import android.widget.ImageView
import android.widget.TextView
import com.chaquo.python.PyObject
import java.nio.ByteBuffer
import java.util.concurrent.Executors

/**
 * Runs one game. The emulator runs on its own thread in this activity's own process (":game"),
 * hands each presented frame over as RGBA bytes, and takes touches in the frame's pixels.
 */
class GameActivity : Activity() {
    private val ui = Handler(Looper.getMainLooper())
    private val touches = Executors.newSingleThreadExecutor()
    private lateinit var image: ImageView
    private lateinit var log: TextView
    private lateinit var py: PyObject
    private var bitmap: Bitmap? = null
    private var frameW = 1136
    private var frameH = 640
    private var frames = 0
    private val lines = ArrayDeque<String>()
    @Volatile private var pending: ByteArray? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        py = MainActivity.pocketpear(this)

        image = ImageView(this).apply {
            scaleType = ImageView.ScaleType.FIT_CENTER
            setBackgroundColor(Color.BLACK)
            setOnTouchListener { v, e -> onTouch(v, e); true }
        }
        log = TextView(this).apply {
            setTextColor(Color.argb(220, 255, 255, 255))
            setShadowLayer(3f, 1f, 1f, Color.BLACK)
            textSize = 11f
            setPadding(12, 8, 12, 8)
        }
        val root = FrameLayout(this)
        root.addView(image, FrameLayout.LayoutParams(-1, -1))
        root.addView(log, FrameLayout.LayoutParams(-1, -2))
        root.addView(Button(this).apply {
            text = "Copy log"
            textSize = 11f
            alpha = 0.6f
            setOnClickListener { copyLog(this@GameActivity, py) }
        }, FrameLayout.LayoutParams(-2, -2, Gravity.TOP or Gravity.END))
        setContentView(root)
        say("Starting ${intent.getStringExtra("name")}... (the first frame takes a few minutes)")

        val app = intent.getStringExtra("app")!!
        Thread({ py.callAttr("run", filesDir.absolutePath, app, Listener()) }, "pocketpear-emu").apply {
            priority = Thread.MAX_PRIORITY
            start()
        }
        log.setOnClickListener { log.visibility = View.INVISIBLE }
    }

    private fun say(line: String) {
        lines.addLast(line)
        while (lines.size > 8) lines.removeFirst()
        log.text = lines.joinToString("\n")
        log.visibility = View.VISIBLE
    }

    inner class Listener {
        @Suppress("unused")
        fun onFrame(rgba: ByteArray, w: Int, h: Int) {
            // Keep only the newest frame; the UI thread draws whatever is newest when it gets to it.
            val first = pending == null
            pending = rgba
            frameW = w; frameH = h
            if (first) ui.post { show() }
        }

        @Suppress("unused")
        fun onLog(line: String) { ui.post { say(line) } }

        @Suppress("unused")
        fun onExit() { ui.post { say("(game stopped - press back)") } }
    }

    private fun show() {
        val data = pending ?: return
        pending = null
        var b = bitmap
        if (b == null || b.width != frameW || b.height != frameH) {
            b = Bitmap.createBitmap(frameW, frameH, Bitmap.Config.ARGB_8888)
            bitmap = b
        }
        b.copyPixelsFromBuffer(ByteBuffer.wrap(data))     // RGBA bytes, as ARGB_8888 stores them
        image.setImageBitmap(b)
        image.invalidate()
        frames++
        if (frames == 1) say("First frame. Tap the screen to play; tap this text to hide it.")
    }

    /** View coordinates -> the game's frame pixels, through FIT_CENTER's scale and offset. */
    private fun onTouch(v: View, e: MotionEvent) {
        val kind = when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> "down"
            MotionEvent.ACTION_MOVE -> "move"
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> "up"
            else -> return
        }
        val s = minOf(v.width.toFloat() / frameW, v.height.toFloat() / frameH)
        val ox = (v.width - frameW * s) / 2
        val oy = (v.height - frameH * s) / 2
        val x = ((e.x - ox) / s).coerceIn(0f, frameW - 1f)
        val y = ((e.y - oy) / s).coerceIn(0f, frameH - 1f)
        touches.execute { py.callAttr("touch", kind, x, y) }
    }

    companion object {
        /** Puts the run log (see log_text) on the clipboard. Used here and on the main screen. */
        fun copyLog(a: Activity, py: PyObject) {
            Thread {
                val text = try {
                    py.callAttr("log_text", a.filesDir.absolutePath).toString()
                } catch (e: Throwable) {
                    "(could not read the log: ${e.message})"
                }
                a.runOnUiThread {
                    if (text.isEmpty()) {
                        Toast.makeText(a, "No log yet - run a game first.", Toast.LENGTH_SHORT).show()
                    } else {
                        val cm = a.getSystemService(android.content.ClipboardManager::class.java)
                        cm.setPrimaryClip(android.content.ClipData.newPlainText("PocketPear log", text))
                        Toast.makeText(a, "Log copied (${text.length / 1024} KB).", Toast.LENGTH_SHORT).show()
                    }
                }
            }.start()
        }
    }

    override fun onDestroy() {
        try { py.callAttr("stop") } catch (_: Throwable) {}
        super.onDestroy()
        // The emulator's state is per process; end this one so the next run starts clean.
        android.os.Process.killProcess(android.os.Process.myPid())
    }
}
