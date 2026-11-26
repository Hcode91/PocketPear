package com.hcode91.pocketpear

import android.app.Activity
import android.app.ActivityOptions
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.Settings
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.View
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.view.WindowManager
import android.widget.FrameLayout
import kotlin.math.max
import kotlin.math.min
import android.widget.ImageView
import android.graphics.BitmapFactory
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.PorterDuff
import android.graphics.PorterDuffXfermode
import android.graphics.RectF
import android.graphics.drawable.Icon
import android.content.pm.ShortcutInfo
import android.content.pm.ShortcutManager
import android.graphics.Outline
import android.view.ViewOutlineProvider
import android.widget.LinearLayout
import android.widget.TextView
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONArray

/** The game list, and importing the firmware and games. */
class MainActivity : Activity() {
    private val ui = Handler(Looper.getMainLooper())
    private lateinit var py: PyObject
    private var busy = false
    private lateinit var banner: Banner
    private lateinit var pager: Pager
    private lateinit var dots: PageDots
    private lateinit var statusBar: StatusBar
    private lateinit var dock: LinearLayout
    private var bannerTap: () -> Unit = {}
    private val icons = HashMap<String, Bitmap>()
    private var ready = false
    private var apps = JSONArray()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        py = pocketpear(this)
        edgeToEdge()

        val root = FrameLayout(this).apply { background = Wallpaper() }
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(col, FrameLayout.LayoutParams(-1, -1))

        statusBar = StatusBar(this)
        col.addView(statusBar, LinearLayout.LayoutParams(-1, dp(44)))

        banner = Banner(this).apply { setOnClickListener { bannerTap() }; visibility = View.GONE }
        col.addView(banner, LinearLayout.LayoutParams(-1, -2).apply { setMargins(dp(10), dp(2), dp(10), dp(6)) })

        pager = Pager(this)
        col.addView(pager, LinearLayout.LayoutParams(-1, 0, 1f))
        dots = PageDots(this)
        pager.onPage = { dots.current = it }
        pager.onResize = { buildPages(apps) }
        col.addView(dots, LinearLayout.LayoutParams(-1, dp(26)))

        // the dock: PocketPear's own tools
        dock = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
            background = Skin.card(this@MainActivity, 0x38FFFFFF, 30f)
            setPadding(dp(6), dp(6), dp(6), dp(2))
        }
        dockTool("Firmware", ToolIcon.Kind.FIRMWARE) { pick(REQ_FIRMWARE) }
        dockTool("Add Game", ToolIcon.Kind.ADD) { pick(REQ_IPA) }
        dockTool("Saves", ToolIcon.Kind.FOLDER) { saves() }
        dockTool("Log", ToolIcon.Kind.LOG) { GameActivity.copyLog(this@MainActivity, py) }
        col.addView(dock, LinearLayout.LayoutParams(-1, -2).apply { setMargins(dp(10), 0, dp(10), dp(10)) })

        // room for the camera hole at the top and the gesture bar at the bottom
        root.setOnApplyWindowInsetsListener { _, ins ->
            var top = 0
            if (Build.VERSION.SDK_INT >= 28) top = ins.displayCutout?.safeInsetTop ?: 0
            @Suppress("DEPRECATION")
            val bottom = ins.systemWindowInsetBottom
            statusBar.layoutParams = statusBar.layoutParams.apply { height = max(dp(34), top) + dp(10) }
            col.setPadding(0, 0, 0, bottom)
            ins
        }
        setContentView(root)
        refresh()
        launchRequested(intent)
    }

    /** Draw behind the system bars and hide Android's status bar: PocketPear draws its own. */
    @Suppress("DEPRECATION")
    private fun edgeToEdge() {
        window.statusBarColor = Color.TRANSPARENT
        window.navigationBarColor = Color.TRANSPARENT
        if (Build.VERSION.SDK_INT >= 28) window.attributes = window.attributes.apply {
            layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES
        }
        window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_LAYOUT_STABLE or
            View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION or
            View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
        if (Build.VERSION.SDK_INT >= 30) window.insetsController?.let {
            it.hide(WindowInsets.Type.statusBars())
            it.systemBarsBehavior = WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) edgeToEdge()          // the status bar comes back after dialogs and swipes
    }

    private fun dockTool(label: String, kind: ToolIcon.Kind, action: () -> Unit) {
        val t = IconTile(this, label, dp(56))
        t.image.setImageDrawable(ToolIcon(kind))
        t.isClickable = true
        t.setOnClickListener { action() }
        dock.addView(t, LinearLayout.LayoutParams(0, -2, 1f))
    }

    private fun saves() {
        if (hasFileAccess(this)) Sheets.alert(this, "Saves",
            "Games keep their saves and settings in Internal storage/PocketPear/data, where you can back them up.")
        else Sheets.alert(this, "Keep saves where you can see them?",
            "PocketPear needs file access to keep games' saves in Internal storage/PocketPear, where you can back them up. Without it they stay private to the app.",
            Sheets.Choice("Not Now"), Sheets.Choice("Allow", preferred = true) { askForFileAccess() })
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        launchRequested(intent)
    }

    /** Another app (BacklogQuest) asking for one game by name or bundle ID: start it. */
    private fun launchRequested(i: Intent?) {
        if (i?.action != POCKET_LAUNCH) return
        val want = i.getStringExtra("game_id")?.trim().orEmpty()
        i.action = null                                      // not again on a rotation
        if (want.isEmpty()) return
        if (!py.callAttr("firmware_ready", filesDir.absolutePath).toBoolean()) {
            toast("Import the firmware first."); return
        }
        val found = py.callAttr("find_app", filesDir.absolutePath, want).toString()
        if (found.isEmpty()) {
            toast("No game called \"$want\" in PocketPear. Import it, or check the name in BacklogQuest matches the one in this list.")
            return
        }
        val a = org.json.JSONObject(found)
        val path = a.getString("path")
        startActivity(Intent(this, GameActivity::class.java)
            .putExtra("app", path).putExtra("name", a.getString("name"))
            .putExtra("realtime", realtime(path)))
    }

    private fun prefs() = getSharedPreferences("games", MODE_PRIVATE)
    private fun realtime(path: String) = prefs().getBoolean("realtime:$path", false)

    // How a landscape game drawn into a portrait picture is turned to stand upright. Most games
    // say so themselves; this is for the ones that don't (and stays "auto" otherwise).
    private val turns = arrayOf("auto", "ccw", "cw", "none")
    private val turnNames = arrayOf("automatic", "anticlockwise", "clockwise", "not turned")
    private fun turnLabel(path: String) = turnNames[turns.indexOf(prefs().getString("turn:$path", "auto")).coerceAtLeast(0)]
    private fun chooseTurn(name: String, path: String) {
        val now = turns.indexOf(prefs().getString("turn:$path", "auto")).coerceAtLeast(0)
        Sheets.actionSheet(this, "Turn $name's picture", turns.indices.map { which ->
            Sheets.Choice(turnNames[which].replaceFirstChar { it.uppercase() }, checked = which == now) {
                prefs().edit().putString("turn:$path", turns[which]).apply()
                toast("$name: ${turnNames[which]}, from its next start.")
            }
        })
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    override fun onResume() {
        super.onResume()
        // back from the all-files-access screen: move saves to the visible folder now
        if (::py.isInitialized) { py = pocketpear(this); refresh() }
    }

    private fun askForFileAccess() {
        if (Build.VERSION.SDK_INT >= 30) {
            try {
                startActivity(Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                    Uri.parse("package:$packageName")))
            } catch (e: Exception) {
                startActivity(Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
            }
        } else {
            requestPermissions(arrayOf(android.Manifest.permission.WRITE_EXTERNAL_STORAGE), 3)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        py = pocketpear(this); refresh()
    }

    private fun refresh() {
        ready = py.callAttr("firmware_ready", filesDir.absolutePath).toBoolean()
        if (!busy) showBanner()
        apps = JSONArray(py.callAttr("list_apps", filesDir.absolutePath).toString())
        if (pager.height > 0) buildPages(apps)      // otherwise once the pager has its size

    }

    private fun showBanner() {
        when {
            !ready -> {
                banner.set("Firmware needed", "Tap here or Firmware in the dock to import the iPhone 5 iOS 6 firmware: the .dmg, a .zip of it, or all its split parts (.001, .002, ...) picked together.")
                bannerTap = { pick(REQ_FIRMWARE) }
                banner.visibility = View.VISIBLE
            }
            !hasFileAccess(this) -> {
                banner.set("Saves are private", "Tap to keep games' saves in Internal storage/PocketPear, where you can back them up.")
                bannerTap = { saves() }
                banner.visibility = View.VISIBLE
            }
            else -> banner.visibility = View.GONE
        }
    }

    /** The games as home screen pages of icons, as many rows as fit. */
    private fun buildPages(apps: JSONArray) {
        if (pager.height == 0) return
        pager.removeAllViews()
        val landscape = pager.width > pager.height
        val cols = if (landscape) 7 else 4
        val tileH = dp(100)
        val rows = max(1, (pager.height - dp(12)) / tileH)
        val perPage = cols * rows
        if (apps.length() == 0) {
            pager.addView(LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                gravity = Gravity.CENTER
                addView(TextView(this@MainActivity).apply {
                    text = "No Games"; textSize = 22f; typeface = Skin.bold; setTextColor(Color.WHITE)
                    gravity = Gravity.CENTER
                })
                addView(TextView(this@MainActivity).apply {
                    text = "Tap Add Game in the dock to import a decrypted .ipa."
                    textSize = 15f; setTextColor(0xCCFFFFFF.toInt()); gravity = Gravity.CENTER
                    setPadding(dp(32), dp(6), dp(32), 0)
                })
            }, FrameLayout.LayoutParams(-1, -1))
        }
        val pageCount = (apps.length() + perPage - 1) / perPage
        for (pg in 0 until pageCount) {
            val page = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(8), dp(12), dp(8), 0)
            }
            var row: LinearLayout? = null
            for (i in pg * perPage until min(apps.length(), (pg + 1) * perPage)) {
                if ((i - pg * perPage) % cols == 0) {
                    row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; weightSum = cols.toFloat() }
                    page.addView(row, LinearLayout.LayoutParams(-1, tileH))
                }
                val a = apps.getJSONObject(i)
                row!!.addView(gameTile(a.getString("name"), a.getString("path")), LinearLayout.LayoutParams(0, -2, 1f))
            }
            pager.addView(page, FrameLayout.LayoutParams(-1, -1))
        }
        dots.count = pageCount
        pager.show(min(pager.page, max(0, pageCount - 1)), false)
    }

    private fun gameTile(name: String, path: String): IconTile {
        val tile = IconTile(this, name, dp(62))
        tile.image.setBackgroundColor(0x40FFFFFF)
        val cached = icons[path]
        if (cached != null) { tile.image.setImageBitmap(cached); tile.image.setBackgroundColor(Color.TRANSPARENT) }
        // the game's own icon, read from its bundle off the UI thread: a crushed PNG is
        // rewritten the first time
        else Thread {
            val file = try { py.callAttr("app_icon", filesDir.absolutePath, path).toString() } catch (e: Exception) { "" }
            val bmp = if (file.isNotEmpty()) BitmapFactory.decodeFile(file) else null
            if (bmp != null) ui.post {
                icons[path] = bmp
                tile.image.setImageBitmap(bmp); tile.image.setBackgroundColor(Color.TRANSPARENT)
            }
        }.start()
        tile.isClickable = true
        tile.setOnClickListener {
            if (!ready) { toast("Import the firmware first."); return@setOnClickListener }
            val v = tile.image
            // opens out of its icon, as on a phone's home screen
            val zoom = ActivityOptions.makeScaleUpAnimation(v, 0, 0, v.width, v.height).toBundle()
            startActivity(Intent(this, GameActivity::class.java)
                .putExtra("app", path).putExtra("name", name)
                .putExtra("realtime", realtime(path)), zoom)
        }
        tile.setOnLongClickListener {
            tile.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
            gameMenu(name, path)
            true
        }
        return tile
    }

    private fun gameMenu(name: String, path: String) {
        val where = py.callAttr("data_path", path).toString()
        val rt = realtime(path)
        Sheets.contextMenu(this, icons[path], name, listOf(
            Sheets.Choice("Add to Home Screen") { addShortcut(name, path) },
            Sheets.Choice(if (rt) "Speed: Real Time" else "Speed: Slow Motion When Behind") {
                val on = !rt
                prefs().edit().putBoolean("realtime:$path", on).apply()
                toast(if (on) "$name keeps real time: proper speed, fewer frames when the phone can't keep up."
                      else "$name slows down when the phone can't keep up: smoother, but in slow motion.")
            },
            Sheets.Choice("Turn Picture: ${turnLabel(path).replaceFirstChar { it.uppercase() }}") { chooseTurn(name, path) },
            Sheets.Choice("Clear Data", destructive = true) {
                Sheets.alert(this, "Clear $name's data?",
                    "Deletes its saves and settings, as reinstalling it did on an iPhone. The game stays installed.\n\n$where",
                    Sheets.Choice("Cancel"),
                    Sheets.Choice("Clear", destructive = true) { py.callAttr("clear_data", path); toast("$name's data cleared.") })
            },
            Sheets.Choice("Remove Game", destructive = true) {
                Sheets.alert(this, "Remove $name?", "Its saves and settings are kept.",
                    Sheets.Choice("Cancel"),
                    Sheets.Choice("Remove", destructive = true) {
                        py.callAttr("remove_app", path)
                        icons.remove(path)
                        getSystemService(ShortcutManager::class.java)?.disableShortcuts(
                            listOf(shortcutId(path)), "$name was removed from PocketPear.")
                        refresh()
                    })
            }))
    }

    private fun shortcutId(path: String) = "game:" + java.io.File(path).name

    /** A home screen icon that starts the game straight away, with the game's own icon. */
    private fun addShortcut(name: String, path: String) {
        val sm = getSystemService(ShortcutManager::class.java)
        if (sm == null || !sm.isRequestPinShortcutSupported) {
            toast("This home screen app doesn't let other apps add icons to it.")
            return
        }
        Thread {
            val file = try { py.callAttr("app_icon", filesDir.absolutePath, path).toString() } catch (e: Exception) { "" }
            val bmp = if (file.isNotEmpty()) BitmapFactory.decodeFile(file) else null
            ui.post {
                val icon = if (bmp != null) Icon.createWithBitmap(roundedIcon(bmp))
                           else Icon.createWithResource(this, R.drawable.ic_launcher)
                val launch = Intent(this, GameActivity::class.java)
                    .setAction(Intent.ACTION_VIEW)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
                    .putExtra("app", path).putExtra("name", name)
                val info = ShortcutInfo.Builder(this, shortcutId(path))
                    .setShortLabel(name.take(25)).setLongLabel(name)
                    .setIcon(icon).setIntent(launch).build()
                try {
                    if (sm.pinnedShortcuts.any { it.id == info.id }) {
                        sm.enableShortcuts(listOf(info.id))
                        sm.updateShortcuts(listOf(info))
                        toast("$name is already on your home screen.")
                    } else sm.requestPinShortcut(info, null)
                } catch (e: Exception) {
                    toast("Couldn't add $name to the home screen: ${e.message}")
                }
            }
        }.start()
    }

    /** The icon with rounded corners, as on the phone, on a square the launcher shows whole. */
    private fun roundedIcon(src: Bitmap): Bitmap {
        val size = 192
        val out = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888)
        val c = Canvas(out)
        val p = Paint(Paint.ANTI_ALIAS_FLAG or Paint.FILTER_BITMAP_FLAG)
        val r = RectF(0f, 0f, size.toFloat(), size.toFloat())
        c.drawRoundRect(r, size * 0.18f, size * 0.18f, p)
        p.xfermode = PorterDuffXfermode(PorterDuff.Mode.SRC_IN)
        c.drawBitmap(src, null, r, p)
        return out
    }

    private fun pick(req: Int) {
        if (busy) return
        val i = Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*")
        if (req == REQ_FIRMWARE) i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
        startActivityForResult(i, req)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (resultCode != RESULT_OK || data == null) return
        val uris = ArrayList<android.net.Uri>()
        val clip = data.clipData
        if (clip != null) for (k in 0 until clip.itemCount) uris.add(clip.getItemAt(k).uri)
        else data.data?.let { uris.add(it) }
        if (uris.isEmpty()) return
        val names = ArrayList<String>()
        val fds = ArrayList<Int>()
        for (u in uris) {
            val pfd = contentResolver.openFileDescriptor(u, "r") ?: continue
            names.add(displayName(u))
            fds.add(pfd.detachFd())               // Python owns and closes these
        }
        if (fds.isEmpty()) return
        busy = true
        banner.set("Importing", "Starting...", 0); banner.visibility = View.VISIBLE; bannerTap = {}
        Thread {
            val msg = try {
                if (requestCode == REQ_FIRMWARE)
                    py.callAttr("import_firmware", fds.toIntArray(), names.toTypedArray(),
                        filesDir.absolutePath, Progress()).toString()
                else
                    py.callAttr("import_ipa", fds[0], filesDir.absolutePath, Progress()).toString()
            } catch (e: Throwable) {
                "Import failed: ${e.message}"
            }
            ui.post { busy = false; toast(msg); refresh() }
        }.start()
    }

    private fun displayName(u: android.net.Uri): String {
        contentResolver.query(u, arrayOf(android.provider.OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
            if (c.moveToFirst()) c.getString(0)?.let { return it }
        }
        return u.lastPathSegment ?: ""
    }

    inner class Progress {
        @Suppress("unused")
        fun onProgress(label: String, pct: Int) {
            ui.post { banner.set("Importing $label", "$pct%", pct) }
        }
    }

    private fun toast(s: String) = Sheets.alert(this, null, s)

    companion object {
        const val REQ_FIRMWARE = 1
        const val REQ_IPA = 2

        /** What BacklogQuest (and other launchers) send, with the game's name or bundle ID in "game_id". */
        const val POCKET_LAUNCH = "com.hcode91.pocket.LAUNCH_GAME"

        fun pocketpear(a: Activity): PyObject {
            if (!Python.isStarted()) Python.start(AndroidPlatform(a))
            val m = Python.getInstance().getModule("pocketpear_android")
            m.callAttr("setup", a.filesDir.absolutePath, a.applicationInfo.nativeLibraryDir, a.cacheDir.absolutePath,
                dataDir(a))
            return m
        }

        fun hasFileAccess(a: Activity): Boolean =
            if (Build.VERSION.SDK_INT >= 30) Environment.isExternalStorageManager()
            else a.checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE) == PackageManager.PERMISSION_GRANTED

        /** Games' saves and settings: Internal storage/PocketPear/data when the player can see
         *  it, else private storage (null). */
        fun dataDir(a: Activity): String? =
            if (hasFileAccess(a)) java.io.File(Environment.getExternalStorageDirectory(), "PocketPear/data").absolutePath
            else null
    }
}
