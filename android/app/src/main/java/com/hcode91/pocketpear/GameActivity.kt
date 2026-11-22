package com.hcode91.pocketpear

import android.app.Activity
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.SurfaceTexture
import android.media.MediaPlayer
import android.view.Surface
import android.view.TextureView
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
    private lateinit var root: FrameLayout
    private lateinit var movieView: TextureView
    private var movieSurface: Surface? = null
    private var player: MediaPlayer? = null
    private var playerId = 0
    private var movieRect: IntArray? = null      // x, y, w, h in frame pixels, and the frame's w, h
    private var videoW = 0
    private var videoH = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        py = MainActivity.pocketpear(this)

        image = ImageView(this).apply {
            scaleType = ImageView.ScaleType.FIT_CENTER
            // transparent where the frame is: a movie plays underneath, in the movie view's place
            setOnTouchListener { v, e -> onTouch(v, e); true }
        }
        movieView = TextureView(this).apply { visibility = View.INVISIBLE }
        log = TextView(this).apply {
            setTextColor(Color.argb(220, 255, 255, 255))
            setShadowLayer(3f, 1f, 1f, Color.BLACK)
            textSize = 11f
            setPadding(12, 8, 12, 8)
        }
        root = FrameLayout(this)
        root.setBackgroundColor(Color.BLACK)
        root.addView(movieView, FrameLayout.LayoutParams(0, 0))
        root.addView(image, FrameLayout.LayoutParams(-1, -1))
        root.addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ -> placeMovie() }
        root.addView(log, FrameLayout.LayoutParams(-1, -2))
        log.translationZ = 2f
        root.addView(Button(this).apply {
            text = "Copy log"
            textSize = 11f
            alpha = 0.6f
            setOnClickListener { copyLog(this@GameActivity, py) }
            translationZ = 2f
        }, FrameLayout.LayoutParams(-2, -2, Gravity.TOP or Gravity.END))
        setContentView(root)
        say("Starting ${intent.getStringExtra("name")}... (the first frame takes a few minutes)")

        val app = intent.getStringExtra("app")!!
        Thread({ py.callAttr("run", filesDir.absolutePath, app, Listener(),
                intent.getBooleanExtra("realtime", false)) }, "pocketpear-emu").apply {
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
        fun onMovie(event: String, id: Int, path: String, repeat: Boolean) {
            ui.post { movie(event, id, path, repeat) }
        }

        @Suppress("unused")
        fun onMovieRect(x: Int, y: Int, w: Int, h: Int, fw: Int, fh: Int) {
            ui.post { movieRect = intArrayOf(x, y, w, h, fw, fh); placeMovie() }
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

    /** Plays a game's movie with the platform's player, in a view under the game's frame. */
    private fun movie(event: String, id: Int, path: String, repeat: Boolean) {
        when (event) {
            "play" -> {
                releasePlayer()
                playerId = id
                movieRect = null            // full screen until the frame shows where its view is
                videoW = 0; videoH = 0
                val mp = MediaPlayer()
                player = mp
                try {
                    mp.setDataSource(path)
                    mp.isLooping = repeat
                    mp.setOnVideoSizeChangedListener { _, w, h -> videoW = w; videoH = h; placeMovie() }
                    mp.setOnPreparedListener {
                        if (player !== mp) return@setOnPreparedListener
                        it.start()
                        note("[movie] host player $id prepared: ${it.videoWidth}x${it.videoHeight}, ${it.duration / 1000.0} s")
                        val secs = it.duration / 1000.0
                        val w = it.videoWidth
                        val h = it.videoHeight
                        touches.execute { py.callAttr("movie_info", id, secs, w, h) }
                    }
                    mp.setOnCompletionListener {
                        if (player === mp && !mp.isLooping) {
                            movieView.visibility = View.INVISIBLE
                            touches.execute { py.callAttr("movie_done", id, false) }
                        }
                    }
                    mp.setOnErrorListener { _, what, extra ->
                        say("Movie could not play ($what/$extra): ${path.substringAfterLast('/')}")
                        if (player === mp) {
                            movieView.visibility = View.INVISIBLE
                            touches.execute { py.callAttr("movie_done", id, true) }
                        }
                        true
                    }
                    // A fresh view per movie: reusing one after its player was released left the
                    // last movie's final frame on screen while the next one played unseen
                    // (Bioshock's menu backdrop stayed up through its whole opening movie).
                    freshMovieView()
                    firstPicture = false
                    movieView.visibility = View.VISIBLE
                    placeMovie()
                    attachSurface(mp)
                } catch (e: Exception) {
                    say("Movie could not play: ${e.message}")
                    releasePlayer()
                    touches.execute { py.callAttr("movie_done", id, true) }
                }
            }
            "pause" -> if (id == playerId) player?.pause()
            "resume" -> if (id == playerId) player?.start()
            "restart" -> if (id == playerId) player?.seekTo(0)
            "repeat" -> if (id == playerId) player?.isLooping = repeat
            "stop" -> if (id == playerId) releasePlayer()
        }
    }

    /** Gives the player the movie view's surface and prepares it once the surface exists
     *  (a player prepared without one can lose its picture on some devices). */
    private fun attachSurface(mp: MediaPlayer) {
        fun begin(t: SurfaceTexture) {
            if (player !== mp) return
            note("[movie] host player $id has its surface (${movieView.width}x${movieView.height})")
            val surface = Surface(t)
            movieSurface = surface
            mp.setSurface(surface)
            mp.prepareAsync()
        }
        val st = movieView.surfaceTexture
        if (st != null) { begin(st); return }
        movieView.surfaceTextureListener = object : TextureView.SurfaceTextureListener {
            override fun onSurfaceTextureAvailable(t: SurfaceTexture, w: Int, h: Int) = begin(t)
            override fun onSurfaceTextureSizeChanged(t: SurfaceTexture, w: Int, h: Int) {}
            override fun onSurfaceTextureDestroyed(t: SurfaceTexture) = true
            override fun onSurfaceTextureUpdated(t: SurfaceTexture) {
                if (!firstPicture) { firstPicture = true; note("[movie] host player $playerId shows its first picture") }
            }
        }
    }

    private var firstPicture = false
    private var lastPlace = ""

    private fun note(line: String) { touches.execute { py.callAttr("note", line) } }

    /** Replaces the movie view with a new, empty one in the same place in the layout. */
    private fun freshMovieView() {
        val index = root.indexOfChild(movieView)
        root.removeView(movieView)
        movieView = TextureView(this).apply { visibility = View.INVISIBLE }
        root.addView(movieView, index.coerceAtLeast(0), FrameLayout.LayoutParams(0, 0))
    }

    private fun releasePlayer() {
        player?.let { try { it.stop() } catch (_: Exception) {}; it.release() }
        player = null
        movieSurface?.release()
        movieSurface = null
        movieView.visibility = View.INVISIBLE
    }

    /** Puts the movie in its view's place in the frame (under the frame, which is transparent
     *  there), or over the whole frame for a fullscreen player, fitted to the video's shape. */
    private fun placeMovie() {
        if (player == null) return
        val vw = root.width.toFloat()
        val vh = root.height.toFloat()
        if (vw <= 0f || vh <= 0f) return
        val r = movieRect
        val fw = (r?.get(4) ?: frameW).toFloat()
        val fh = (r?.get(5) ?: frameH).toFloat()
        val s = minOf(vw / fw, vh / fh)
        val ox = (vw - fw * s) / 2
        val oy = (vh - fh * s) / 2
        val full = r == null || r[2] <= 0 || r[3] <= 0
        var x = if (full) ox else ox + r!![0] * s
        var y = if (full) oy else oy + r!![1] * s
        var w = if (full) fw * s else r!![2] * s
        var h = if (full) fh * s else r!![3] * s
        if (videoW > 0 && videoH > 0) {             // aspect fit, as MPMovieScalingModeAspectFit
            val k = minOf(w / videoW, h / videoH)
            x += (w - videoW * k) / 2; y += (h - videoH * k) / 2
            w = videoW * k; h = videoH * k
        }
        val lp = FrameLayout.LayoutParams(w.toInt().coerceAtLeast(1), h.toInt().coerceAtLeast(1))
        lp.leftMargin = x.toInt(); lp.topMargin = y.toInt()
        movieView.layoutParams = lp
        // under the frame when it has a hole for it, over it when the player is fullscreen
        movieView.translationZ = if (full) 1f else 0f
        val where = "[movie] host view at ${x.toInt()},${y.toInt()} ${w.toInt()}x${h.toInt()} ${if (full) "over" else "under"} the frame, ${if (movieView.visibility == View.VISIBLE) "visible" else "hidden"}"
        if (where != lastPlace) { lastPlace = where; note(where) }
    }

    /** Every finger on the screen, in the game's frame pixels (view coordinates through
     *  FIT_CENTER's scale and offset), so the game sees several touches at once. */
    private fun onTouch(v: View, e: MotionEvent) {
        val lifted = when (e.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_POINTER_DOWN, MotionEvent.ACTION_MOVE -> -1
            MotionEvent.ACTION_UP, MotionEvent.ACTION_POINTER_UP -> e.getPointerId(e.actionIndex)
            MotionEvent.ACTION_CANCEL -> -2          // every finger goes
            else -> return
        }
        val s = minOf(v.width.toFloat() / frameW, v.height.toFloat() / frameH)
        val ox = (v.width - frameW * s) / 2
        val oy = (v.height - frameH * s) / 2
        val n = if (lifted == -2) 0 else e.pointerCount
        val ids = IntArray(n) { e.getPointerId(it) }
        val xs = FloatArray(n) { ((e.getX(it) - ox) / s).coerceIn(0f, frameW - 1f) }
        val ys = FloatArray(n) { ((e.getY(it) - oy) / s).coerceIn(0f, frameH - 1f) }
        touches.execute { py.callAttr("touches", ids, xs, ys, lifted) }
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

    private var pausedByActivity = false

    override fun onPause() {
        super.onPause()
        player?.let { if (it.isPlaying) { it.pause(); pausedByActivity = true } }
    }

    override fun onResume() {
        super.onResume()
        if (pausedByActivity) player?.let { try { it.start() } catch (_: Exception) {} }
        pausedByActivity = false
    }

    override fun onDestroy() {
        releasePlayer()
        try { py.callAttr("stop") } catch (_: Throwable) {}
        super.onDestroy()
        // The emulator's state is per process; end this one so the next run starts clean.
        android.os.Process.killProcess(android.os.Process.myPid())
    }
}
