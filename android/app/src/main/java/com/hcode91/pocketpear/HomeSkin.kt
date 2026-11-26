package com.hcode91.pocketpear

import android.app.Activity
import android.app.Dialog
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.ColorFilter
import android.graphics.LinearGradient
import android.graphics.Outline
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PixelFormat
import android.graphics.RadialGradient
import android.graphics.RectF
import android.graphics.Shader
import android.graphics.Typeface
import android.graphics.drawable.ColorDrawable
import android.graphics.drawable.Drawable
import android.graphics.drawable.GradientDrawable
import android.os.BatteryManager
import android.os.Handler
import android.os.Looper
import android.text.TextUtils
import android.text.format.DateFormat
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.ViewGroup
import android.view.ViewOutlineProvider
import android.view.Window
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/*
 * PocketPear's phone-style look: a home screen of game icons over a wallpaper, a status bar,
 * a dock, page dots, notification-style banners, context menus and alerts. Everything here is
 * drawn by PocketPear itself (shapes, gradients, the phone's own font): no images, icons or
 * fonts from any phone maker.
 */

internal fun Context.dpf(v: Float) = v * resources.displayMetrics.density
internal fun Context.dpi(v: Int) = (v * resources.displayMetrics.density).toInt()

object Skin {
    const val BLUE = 0xFF0A84FF.toInt()
    const val RED = 0xFFFF453A.toInt()
    const val CARD = 0xF0262629.toInt()       // menus and alerts
    const val HAIRLINE = 0xFF3D3D41.toInt()
    const val SUBTLE = 0xFFB4B4BA.toInt()
    const val ICON_CORNER = 0.225f            // corner radius as a fraction of the icon's size

    val regular: Typeface = Typeface.create("sans-serif", Typeface.NORMAL)
    val medium: Typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
    val bold: Typeface = Typeface.create("sans-serif", Typeface.BOLD)

    fun round(v: View, radius: Float) {
        v.outlineProvider = object : ViewOutlineProvider() {
            override fun getOutline(view: View, o: Outline) = o.setRoundRect(0, 0, view.width, view.height, radius)
        }
        v.clipToOutline = true
    }

    /** Rounded like a home screen icon, whatever size the view ends up. */
    fun iconShape(v: View) {
        v.outlineProvider = object : ViewOutlineProvider() {
            override fun getOutline(view: View, o: Outline) =
                o.setRoundRect(0, 0, view.width, view.height, view.width * ICON_CORNER)
        }
        v.clipToOutline = true
    }

    fun card(c: Context, color: Int, radius: Float) = GradientDrawable().apply {
        setColor(color); cornerRadius = c.dpf(radius)
    }
}

/** The wallpaper: a deep night-blue gradient with soft blobs of pear green, teal and violet. */
class Wallpaper : Drawable() {
    private val p = Paint(Paint.ANTI_ALIAS_FLAG)
    override fun draw(c: Canvas) {
        val w = bounds.width().toFloat(); val h = bounds.height().toFloat()
        p.shader = LinearGradient(0f, 0f, w * 0.3f, h, 0xFF101634.toInt(), 0xFF2B1846.toInt(), Shader.TileMode.CLAMP)
        c.drawRect(bounds, p)
        blob(c, w * 0.15f, h * 0.18f, max(w, h) * 0.55f, 0x8C8FC93A.toInt())
        blob(c, w * 0.95f, h * 0.42f, max(w, h) * 0.50f, 0x7A1FA6A0.toInt())
        blob(c, w * 0.30f, h * 0.95f, max(w, h) * 0.60f, 0x80A43BC7.toInt())
        blob(c, w * 0.80f, h * 0.05f, max(w, h) * 0.35f, 0x55E8B04A.toInt())
        p.shader = null
    }
    private fun blob(c: Canvas, x: Float, y: Float, r: Float, color: Int) {
        p.shader = RadialGradient(x, y, r, color, color and 0x00FFFFFF, Shader.TileMode.CLAMP)
        c.drawCircle(x, y, r, p)
    }
    override fun setAlpha(alpha: Int) {}
    override fun setColorFilter(cf: ColorFilter?) {}
    @Deprecated("Deprecated in Java")
    override fun getOpacity() = PixelFormat.OPAQUE
}

/** Time on the left, battery on the right, as drawn by the phone itself; the middle is left
 *  free for the camera hole. */
class StatusBar(c: Context) : View(c) {
    private val ui = Handler(Looper.getMainLooper())
    private val text = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE; typeface = Skin.bold; textSize = c.dpf(15f)
    }
    private val small = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE; typeface = Skin.medium; textSize = c.dpf(12f)
    }
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0x99FFFFFF.toInt(); style = Paint.Style.STROKE; strokeWidth = c.dpf(1.2f)
    }
    private val fill = Paint(Paint.ANTI_ALIAS_FLAG)
    private var level = -1
    private var charging = false
    private val tick = object : Runnable {
        override fun run() { invalidate(); ui.postDelayed(this, 15_000) }
    }
    private val battery = object : BroadcastReceiver() {
        override fun onReceive(ctx: Context, i: Intent) {
            val l = i.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
            val s = i.getIntExtra(BatteryManager.EXTRA_SCALE, 100)
            level = if (l >= 0 && s > 0) l * 100 / s else -1
            val st = i.getIntExtra(BatteryManager.EXTRA_STATUS, 0)
            charging = st == BatteryManager.BATTERY_STATUS_CHARGING || st == BatteryManager.BATTERY_STATUS_FULL
            invalidate()
        }
    }
    var sidePad = c.dpf(28f)

    override fun onAttachedToWindow() {
        super.onAttachedToWindow()
        context.registerReceiver(battery, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        ui.post(tick)
    }
    override fun onDetachedFromWindow() {
        super.onDetachedFromWindow()
        try { context.unregisterReceiver(battery) } catch (_: Exception) {}
        ui.removeCallbacks(tick)
    }

    override fun onDraw(c: Canvas) {
        val mid = height / 2f
        val time = DateFormat.getTimeFormat(context).format(java.util.Date())
            .replace(" AM", "").replace(" PM", "").replace(" am", "").replace(" pm", "")
        c.drawText(time, sidePad, mid + text.textSize * 0.36f, text)
        // battery: outline, fill to the level (green while charging, red when low), little cap
        val bw = context.dpf(25f); val bh = context.dpf(12f)
        val right = width - sidePad - context.dpf(3f)
        val r = RectF(right - bw, mid - bh / 2, right, mid + bh / 2)
        c.drawRoundRect(r, context.dpf(3.5f), context.dpf(3.5f), line)
        fill.color = 0x99FFFFFF.toInt()
        c.drawRoundRect(RectF(r.right + context.dpf(1.5f), mid - context.dpf(2.5f), r.right + context.dpf(3f), mid + context.dpf(2.5f)),
            context.dpf(1f), context.dpf(1f), fill)
        val lv = if (level < 0) 100 else level
        val inset = context.dpf(2f)
        val inner = RectF(r.left + inset, r.top + inset, r.left + inset + (r.width() - 2 * inset) * lv / 100f, r.bottom - inset)
        fill.color = when { charging -> 0xFF34C759.toInt(); lv <= 20 -> Skin.RED; else -> Color.WHITE }
        c.drawRoundRect(inner, context.dpf(2f), context.dpf(2f), fill)
        if (level >= 0) {
            val s = "$level%"
            c.drawText(s, r.left - context.dpf(5f) - small.measureText(s), mid + small.textSize * 0.36f, small)
        }
    }
}

/** PocketPear's own tool icons for the dock: a coloured gradient tile with a white glyph. */
class ToolIcon(private val kind: Kind) : Drawable() {
    enum class Kind { FIRMWARE, ADD, FOLDER, LOG }
    private val p = Paint(Paint.ANTI_ALIAS_FLAG)
    private val g = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE }
    override fun draw(c: Canvas) {
        val b = RectF(bounds); val s = b.width()
        val (top, bottom) = when (kind) {
            Kind.FIRMWARE -> 0xFF8E9AAF.toInt() to 0xFF4A5468.toInt()
            Kind.ADD -> 0xFFB9E05A.toInt() to 0xFF5E9E1E.toInt()
            Kind.FOLDER -> 0xFF5AC8FA.toInt() to 0xFF1E7FD8.toInt()
            Kind.LOG -> 0xFFFFB648.toInt() to 0xFFE0662A.toInt()
        }
        p.shader = LinearGradient(0f, b.top, 0f, b.bottom, top, bottom, Shader.TileMode.CLAMP)
        c.drawRoundRect(b, s * Skin.ICON_CORNER, s * Skin.ICON_CORNER, p)
        p.shader = null
        val u = s / 100f
        fun r(l: Float, t: Float, rr: Float, bb: Float) = RectF(b.left + l * u, b.top + t * u, b.left + rr * u, b.top + bb * u)
        g.style = Paint.Style.FILL; g.color = Color.WHITE
        when (kind) {
            Kind.FIRMWARE -> {          // a chip with pins
                c.drawRoundRect(r(30f, 30f, 70f, 70f), 6 * u, 6 * u, g)
                for (i in 0 until 4) {
                    val o = 35f + i * 9f
                    c.drawRect(r(o, 20f, o + 4f, 30f), g); c.drawRect(r(o, 70f, o + 4f, 80f), g)
                    c.drawRect(r(20f, o, 30f, o + 4f), g); c.drawRect(r(70f, o, 80f, o + 4f), g)
                }
                g.color = bottom
                c.drawRoundRect(r(40f, 40f, 60f, 60f), 3 * u, 3 * u, g)
            }
            Kind.ADD -> {               // a game pad with a plus
                c.drawRoundRect(r(18f, 34f, 82f, 70f), 18 * u, 18 * u, g)
                g.color = bottom
                c.drawRect(r(28f, 49f, 42f, 55f), g); c.drawRect(r(32f, 45f, 38f, 59f), g)
                c.drawCircle(b.left + 64 * u, b.top + 47 * u, 4 * u, g)
                c.drawCircle(b.left + 72 * u, b.top + 55 * u, 4 * u, g)
            }
            Kind.FOLDER -> {            // a folder
                val path = Path().apply {
                    moveTo(b.left + 20 * u, b.top + 32 * u); lineTo(b.left + 42 * u, b.top + 32 * u)
                    lineTo(b.left + 48 * u, b.top + 38 * u); lineTo(b.left + 80 * u, b.top + 38 * u)
                    lineTo(b.left + 80 * u, b.top + 72 * u); lineTo(b.left + 20 * u, b.top + 72 * u); close()
                }
                c.drawPath(path, g)
                g.color = 0x33000000
                c.drawRect(r(20f, 44f, 80f, 46f), g)
            }
            Kind.LOG -> {               // a page of text
                c.drawRoundRect(r(28f, 18f, 72f, 82f), 6 * u, 6 * u, g)
                g.color = bottom
                for (i in 0 until 5) c.drawRect(r(36f, 30f + i * 10f, if (i == 4) 54f else 64f, 34f + i * 10f), g)
            }
        }
    }
    override fun setAlpha(alpha: Int) {}
    override fun setColorFilter(cf: ColorFilter?) {}
    @Deprecated("Deprecated in Java")
    override fun getOpacity() = PixelFormat.TRANSLUCENT
}

/** An icon with its name underneath, dimming while pressed. */
class IconTile(c: Context, label: String, iconSize: Int, showLabel: Boolean = true) : LinearLayout(c) {
    val image = ImageView(c).apply {
        scaleType = ImageView.ScaleType.CENTER_CROP
        Skin.iconShape(this)
        elevation = c.dpf(3f)
    }
    init {
        orientation = VERTICAL
        gravity = Gravity.CENTER_HORIZONTAL
        clipChildren = false; clipToPadding = false
        setPadding(0, c.dpi(6), 0, c.dpi(6))
        addView(image, LayoutParams(iconSize, iconSize))
        if (showLabel) addView(TextView(c).apply {
            text = label
            textSize = 11.5f
            typeface = Skin.regular
            setTextColor(Color.WHITE)
            setShadowLayer(c.dpf(2f), 0f, c.dpf(0.5f), 0x99000000.toInt())
            maxLines = 1
            ellipsize = TextUtils.TruncateAt.END
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(0, c.dpi(5), 0, 0)
        }, LayoutParams(iconSize + c.dpi(18), -2))
    }
    override fun dispatchSetPressed(pressed: Boolean) {
        super.dispatchSetPressed(pressed)
        image.animate().alpha(if (pressed) 0.55f else 1f).setDuration(if (pressed) 60 else 160).start()
    }
}

/** Home screen pages side by side: swipe sideways to change page; taps and long presses go
 *  through to the icons. */
class Pager(c: Context) : FrameLayout(c) {
    private val slop = ViewConfiguration.get(c).scaledTouchSlop
    private var downX = 0f; private var downY = 0f; private var dragging = false
    var page = 0; private set
    var onPage: (Int) -> Unit = {}
    var onResize: () -> Unit = {}
    private val pages get() = (0 until childCount).map { getChildAt(it) }

    fun show(p: Int, animate: Boolean = true) {
        page = p.coerceIn(0, max(0, childCount - 1))
        pages.forEachIndexed { i, v ->
            val x = (i - page) * width.toFloat()
            if (animate) v.animate().translationX(x).setDuration(260).start() else v.translationX = x
        }
        onPage(page)
    }

    override fun onSizeChanged(w: Int, h: Int, ow: Int, oh: Int) {
        super.onSizeChanged(w, h, ow, oh)
        if (h != oh || w != ow) post { onResize() } else show(page, false)
    }

    override fun onInterceptTouchEvent(e: MotionEvent): Boolean {
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> { downX = e.x; downY = e.y; dragging = false }
            MotionEvent.ACTION_MOVE -> if (!dragging && childCount > 1) {
                val dx = e.x - downX; val dy = e.y - downY
                if (abs(dx) > slop && abs(dx) > abs(dy)) { dragging = true; downX = e.x }
            }
        }
        return dragging
    }

    override fun onTouchEvent(e: MotionEvent): Boolean {
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> { downX = e.x; return true }
            MotionEvent.ACTION_MOVE -> {
                var dx = e.x - downX
                if ((page == 0 && dx > 0) || (page == childCount - 1 && dx < 0)) dx /= 3f   // rubber band
                pages.forEachIndexed { i, v -> v.translationX = (i - page) * width + dx }
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                val dx = e.x - downX
                val next = when { dx < -width / 5 -> page + 1; dx > width / 5 -> page - 1; else -> page }
                dragging = false
                show(next)
            }
        }
        return true
    }
}

/** A row of page dots. */
class PageDots(c: Context) : View(c) {
    var count = 1; set(v) { field = v; invalidate() }
    var current = 0; set(v) { field = v; invalidate() }
    private val p = Paint(Paint.ANTI_ALIAS_FLAG)
    override fun onDraw(c: Canvas) {
        if (count < 2) return
        val gap = context.dpf(15f); val r = context.dpf(3.5f)
        val x0 = width / 2f - gap * (count - 1) / 2f
        for (i in 0 until count) {
            p.color = if (i == current) Color.WHITE else 0x66FFFFFF
            c.drawCircle(x0 + i * gap, height / 2f, r, p)
        }
    }
}

/** A notification-style banner: small icon, title and a line of text, optional progress. */
class Banner(c: Context) : LinearLayout(c) {
    private val title = TextView(c).apply {
        textSize = 13f; typeface = Skin.bold; setTextColor(Color.WHITE); text = "PocketPear"
    }
    private val body = TextView(c).apply { textSize = 13.5f; typeface = Skin.regular; setTextColor(0xFFE8E8EC.toInt()) }
    private val bar = View(c).apply { background = Skin.card(c, Color.WHITE, 2f) }
    private val track = FrameLayout(c).apply {
        background = Skin.card(c, 0x33FFFFFF, 2f)
        addView(bar, FrameLayout.LayoutParams(0, -1))
        visibility = GONE
    }
    init {
        orientation = HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        background = Skin.card(c, 0xB8202024.toInt(), 22f)
        setPadding(c.dpi(14), c.dpi(12), c.dpi(16), c.dpi(12))
        addView(ImageView(c).apply {
            setImageResource(R.drawable.ic_launcher); scaleType = ImageView.ScaleType.CENTER_CROP
            Skin.iconShape(this)
        }, LayoutParams(c.dpi(36), c.dpi(36)))
        addView(LinearLayout(c).apply {
            orientation = VERTICAL
            setPadding(c.dpi(12), 0, 0, 0)
            addView(title); addView(body)
            addView(track, LayoutParams(-1, c.dpi(4)).apply { topMargin = c.dpi(8) })
        }, LayoutParams(0, -2, 1f))
    }
    fun set(heading: String, text: String, progress: Int = -1) {
        title.text = heading; body.text = text
        track.visibility = if (progress >= 0) VISIBLE else GONE
        if (progress >= 0) track.post {
            bar.layoutParams = (bar.layoutParams as FrameLayout.LayoutParams).apply { width = track.width * min(100, progress) / 100 }
            bar.requestLayout()
        }
    }
}

/** Alerts, action sheets and context menus in the phone style. */
object Sheets {
    class Choice(val label: String, val destructive: Boolean = false, val preferred: Boolean = false,
                 val checked: Boolean = false, val action: () -> Unit = {})

    private fun dialog(a: Activity, content: View, gravity: Int, dim: Float = 0.45f): Dialog {
        val d = Dialog(a)
        d.requestWindowFeature(Window.FEATURE_NO_TITLE)
        d.setContentView(content)
        d.window?.apply {
            setBackgroundDrawable(ColorDrawable(Color.TRANSPARENT))
            setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            setGravity(gravity)
            setDimAmount(dim)
            attributes = attributes.apply { windowAnimations = android.R.style.Animation_Dialog }
        }
        return d
    }

    private fun row(a: Activity, ch: Choice, center: Boolean, bold: Boolean, onTap: () -> Unit) = TextView(a).apply {
        text = if (ch.checked) "${ch.label}   ✓" else ch.label
        textSize = 17f
        typeface = if (bold || ch.preferred) Skin.bold else Skin.regular
        setTextColor(if (ch.destructive) Skin.RED else if (center) Skin.BLUE else Color.WHITE)
        gravity = if (center) Gravity.CENTER else Gravity.CENTER_VERTICAL or Gravity.START
        setPadding(a.dpi(18), 0, a.dpi(18), 0)
        minHeight = a.dpi(46)
        isClickable = true
        background = android.graphics.drawable.RippleDrawable(
            android.content.res.ColorStateList.valueOf(0x33FFFFFF), null, ColorDrawable(Color.WHITE))
        setOnClickListener { onTap() }
    }

    private fun hairline(a: Activity) = View(a).apply { setBackgroundColor(Skin.HAIRLINE) }

    /** A centred alert: title, message, and its buttons side by side (two) or stacked. */
    fun alert(a: Activity, title: String?, message: String?, vararg buttons: Choice) {
        val choices = if (buttons.isEmpty()) arrayOf(Choice("OK", preferred = true)) else buttons
        val box = LinearLayout(a).apply {
            orientation = LinearLayout.VERTICAL
            background = Skin.card(a, Skin.CARD, 14f)
            Skin.round(this, a.dpf(14f))
        }
        val text = LinearLayout(a).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(a.dpi(18), a.dpi(20), a.dpi(18), a.dpi(18))
        }
        if (title != null) text.addView(TextView(a).apply {
            this.text = title; textSize = 17f; typeface = Skin.bold
            setTextColor(Color.WHITE); gravity = Gravity.CENTER
        })
        if (message != null) text.addView(TextView(a).apply {
            this.text = message; textSize = 13.5f; typeface = Skin.regular
            setTextColor(0xFFE2E2E6.toInt()); gravity = Gravity.CENTER
            setPadding(0, if (title != null) a.dpi(4) else 0, 0, 0)
        })
        box.addView(text)
        lateinit var d: Dialog
        box.addView(hairline(a), LinearLayout.LayoutParams(-1, 1))
        if (choices.size == 2) {
            val r = LinearLayout(a).apply { orientation = LinearLayout.HORIZONTAL }
            choices.forEachIndexed { i, ch ->
                if (i == 1) r.addView(hairline(a), LinearLayout.LayoutParams(1, -1))
                r.addView(row(a, ch, true, false) { d.dismiss(); ch.action() }, LinearLayout.LayoutParams(0, -2, 1f))
            }
            box.addView(r)
        } else choices.forEachIndexed { i, ch ->
            if (i > 0) box.addView(hairline(a), LinearLayout.LayoutParams(-1, 1))
            box.addView(row(a, ch, true, false) { d.dismiss(); ch.action() }, LinearLayout.LayoutParams(-1, -2))
        }
        val holder = FrameLayout(a).apply {
            addView(box, FrameLayout.LayoutParams(a.dpi(280), -2, Gravity.CENTER))
        }
        d = dialog(a, holder, Gravity.CENTER)
        d.show()
    }

    /** A sheet from the bottom: a group of choices with an optional title, and Cancel apart. */
    fun actionSheet(a: Activity, title: String?, choices: List<Choice>) {
        lateinit var d: Dialog
        val col = LinearLayout(a).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(a.dpi(10), 0, a.dpi(10), a.dpi(12))
        }
        val group = LinearLayout(a).apply {
            orientation = LinearLayout.VERTICAL
            background = Skin.card(a, Skin.CARD, 14f); Skin.round(this, a.dpf(14f))
        }
        if (title != null) {
            group.addView(TextView(a).apply {
                text = title; textSize = 13f; typeface = Skin.medium; setTextColor(Skin.SUBTLE)
                gravity = Gravity.CENTER; setPadding(a.dpi(16), a.dpi(14), a.dpi(16), a.dpi(14))
            })
        }
        choices.forEachIndexed { i, ch ->
            if (i > 0 || title != null) group.addView(hairline(a), LinearLayout.LayoutParams(-1, 1))
            group.addView(row(a, ch, true, false) { d.dismiss(); ch.action() }, LinearLayout.LayoutParams(-1, a.dpi(56)))
        }
        col.addView(group)
        val cancel = row(a, Choice("Cancel"), true, true) { d.dismiss() }.apply {
            background = Skin.card(a, Skin.CARD, 14f); Skin.round(this, a.dpf(14f))
        }
        col.addView(cancel, LinearLayout.LayoutParams(-1, a.dpi(56)).apply { topMargin = a.dpi(8) })
        d = dialog(a, col, Gravity.BOTTOM)
        d.window?.setWindowAnimations(android.R.style.Animation_InputMethod)
        d.show()
    }

    /** A long-press menu: the icon lifted and enlarged, its name, and a menu card under it. */
    fun contextMenu(a: Activity, icon: Bitmap?, name: String, choices: List<Choice>) {
        lateinit var d: Dialog
        val col = LinearLayout(a).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(a.dpi(24), a.dpi(24), a.dpi(24), a.dpi(24))
            setOnClickListener { d.dismiss() }
        }
        col.addView(ImageView(a).apply {
            if (icon != null) setImageBitmap(icon) else setImageResource(R.drawable.ic_launcher)
            scaleType = ImageView.ScaleType.CENTER_CROP
            Skin.iconShape(this); elevation = a.dpf(10f)
        }, LinearLayout.LayoutParams(a.dpi(92), a.dpi(92)))
        col.addView(TextView(a).apply {
            text = name; textSize = 15f; typeface = Skin.medium; setTextColor(Color.WHITE)
            gravity = Gravity.CENTER; setPadding(0, a.dpi(10), 0, a.dpi(14))
        })
        val menu = LinearLayout(a).apply {
            orientation = LinearLayout.VERTICAL
            background = Skin.card(a, Skin.CARD, 14f); Skin.round(this, a.dpf(14f))
        }
        choices.forEachIndexed { i, ch ->
            if (i > 0) menu.addView(hairline(a), LinearLayout.LayoutParams(-1, 1))
            menu.addView(row(a, ch, false, false) { d.dismiss(); ch.action() }, LinearLayout.LayoutParams(-1, -2))
        }
        col.addView(menu, LinearLayout.LayoutParams(a.dpi(270), -2))
        d = dialog(a, col, Gravity.CENTER, 0.6f)
        d.show()
        col.scaleX = 0.85f; col.scaleY = 0.85f; col.alpha = 0f
        col.animate().scaleX(1f).scaleY(1f).alpha(1f).setDuration(180).start()
    }
}
