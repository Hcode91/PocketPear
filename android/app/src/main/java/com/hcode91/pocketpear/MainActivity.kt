package com.hcode91.pocketpear

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.Settings
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.widget.Button
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
import android.widget.ScrollView
import android.widget.TextView
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONArray

/** The game list, and importing the firmware and games. */
class MainActivity : Activity() {
    private val ui = Handler(Looper.getMainLooper())
    private lateinit var status: TextView
    private lateinit var list: LinearLayout
    private lateinit var py: PyObject
    private var busy = false
    private lateinit var access: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        py = pocketpear(this)

        val pad = dp(16)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
            setBackgroundColor(Color.rgb(24, 24, 30))
        }
        root.addView(TextView(this).apply {
            text = "PocketPear"
            textSize = 26f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(Color.rgb(181, 211, 61))
        })
        root.addView(TextView(this).apply {
            text = "Prototype: runs iOS 6 games on the real iOS libraries. Very slow for now - a few game seconds a minute."
            setTextColor(Color.LTGRAY)
            setPadding(0, dp(4), 0, dp(12))
        })
        status = TextView(this).apply { setTextColor(Color.WHITE) }
        root.addView(status)

        // stacked, full width: side by side, a long label pushes the other button off screen
        val buttons = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        buttons.addView(Button(this).apply {
            text = "Import firmware"
            setOnClickListener { pick(REQ_FIRMWARE) }
        }, LinearLayout.LayoutParams(-1, -2))
        buttons.addView(Button(this).apply {
            text = "Import game (.ipa)"
            setOnClickListener { pick(REQ_IPA) }
        }, LinearLayout.LayoutParams(-1, -2))
        access = Button(this).apply {
            text = "Allow file access (saves go in Internal storage/PocketPear)"
            setOnClickListener { askForFileAccess() }
        }
        buttons.addView(access, LinearLayout.LayoutParams(-1, -2))
        buttons.addView(Button(this).apply {
            text = "Copy last game's log"
            setOnClickListener { GameActivity.copyLog(this@MainActivity, py) }
        }, LinearLayout.LayoutParams(-1, -2))
        root.addView(buttons)

        list = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(ScrollView(this).apply { addView(list) }, LinearLayout.LayoutParams(-1, 0, 1f))
        setContentView(root)
        refresh()
        launchRequested(intent)
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
        access.visibility = if (hasFileAccess(this)) View.GONE else View.VISIBLE
        val ready = py.callAttr("firmware_ready", filesDir.absolutePath).toBoolean()
        status.text = if (ready) "Firmware: ready (iPhone 5, iOS 6)" else
            "Firmware: not imported. Import the iPhone 5 iOS 6 firmware first: the .dmg, a .zip of it, or all its split parts (.001, .002, ...) picked together."
        list.removeAllViews()
        val apps = JSONArray(py.callAttr("list_apps", filesDir.absolutePath).toString())
        if (apps.length() == 0) {
            list.addView(TextView(this).apply {
                text = "No games yet. Import a decrypted .ipa."
                setTextColor(Color.GRAY)
                setPadding(0, dp(16), 0, 0)
            })
        }
        for (i in 0 until apps.length()) {
            val a = apps.getJSONObject(i)
            val name = a.getString("name")
            val path = a.getString("path")
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(dp(8), dp(10), dp(8), dp(10))
            }
            // the game's own icon, rounded like on the home screen; loaded off the UI thread,
            // since a crushed PNG is rewritten the first time
            val icon = ImageView(this).apply {
                scaleType = ImageView.ScaleType.CENTER_CROP
                setBackgroundColor(Color.DKGRAY)
                outlineProvider = object : ViewOutlineProvider() {
                    override fun getOutline(v: View, o: Outline) = o.setRoundRect(0, 0, v.width, v.height, v.width * 0.18f)
                }
                clipToOutline = true
            }
            row.addView(icon, LinearLayout.LayoutParams(dp(56), dp(56)))
            Thread {
                val file = try { py.callAttr("app_icon", filesDir.absolutePath, path).toString() } catch (e: Exception) { "" }
                val bmp = if (file.isNotEmpty()) BitmapFactory.decodeFile(file) else null
                if (bmp != null) ui.post { icon.setImageBitmap(bmp); icon.setBackgroundColor(Color.TRANSPARENT) }
            }.start()
            row.addView(TextView(this).apply {
                text = name
                textSize = 20f
                setTextColor(Color.WHITE)
                setPadding(dp(14), 0, 0, 0)
            }, LinearLayout.LayoutParams(0, -2, 1f))
            list.addView(row.apply {
                setOnClickListener {
                    if (!ready) { toast("Import the firmware first."); return@setOnClickListener }
                    startActivity(Intent(this@MainActivity, GameActivity::class.java)
                        .putExtra("app", path).putExtra("name", name)
                        .putExtra("realtime", realtime(path)))
                }
                setOnLongClickListener {
                    val where = py.callAttr("data_path", path).toString()
                    val clock = if (realtime(path)) "Game speed: real time (tap for slow motion)"
                                else "Game speed: slow motion when behind (tap for real time)"
                    AlertDialog.Builder(this@MainActivity).setTitle(name)
                        .setItems(arrayOf("Clear game data", "Remove game", clock, "Add to home screen")) { _, which ->
                            if (which == 3) addShortcut(name, path)
                            else if (which == 2) {
                                val on = !realtime(path)
                                prefs().edit().putBoolean("realtime:$path", on).apply()
                                toast(if (on) "$name keeps real time: proper speed, fewer frames when the phone can't keep up."
                                      else "$name slows down when the phone can't keep up: smoother, but in slow motion.")
                            }
                            else if (which == 0) AlertDialog.Builder(this@MainActivity)
                                .setTitle("Clear $name's data?")
                                .setMessage("Deletes its saves and settings, as reinstalling it did on an iPhone. The game stays installed.\n\n$where")
                                .setPositiveButton("Clear") { _, _ -> py.callAttr("clear_data", path); toast("$name's data cleared.") }
                                .setNegativeButton("Cancel", null).show()
                            else AlertDialog.Builder(this@MainActivity).setTitle("Remove $name?")
                                .setMessage("Its saves and settings are kept.")
                                .setPositiveButton("Remove") { _, _ ->
                                    py.callAttr("remove_app", path)
                                    getSystemService(ShortcutManager::class.java)?.disableShortcuts(
                                        listOf(shortcutId(path)), "$name was removed from PocketPear.")
                                    refresh()
                                }
                                .setNegativeButton("Cancel", null).show()
                        }.show()
                    true
                }
            })
            list.addView(View(this).apply { setBackgroundColor(Color.DKGRAY) }, LinearLayout.LayoutParams(-1, 1))
        }
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
        status.text = "Importing..."
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
            ui.post { status.text = "Importing $label: $pct%" }
        }
    }

    private fun toast(s: String) = AlertDialog.Builder(this).setMessage(s).setPositiveButton("OK", null).show()

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
