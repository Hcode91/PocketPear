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
            list.addView(TextView(this).apply {
                text = name
                textSize = 20f
                setTextColor(Color.WHITE)
                setPadding(dp(8), dp(14), dp(8), dp(14))
                gravity = Gravity.CENTER_VERTICAL
                setOnClickListener {
                    if (!ready) { toast("Import the firmware first."); return@setOnClickListener }
                    startActivity(Intent(this@MainActivity, GameActivity::class.java)
                        .putExtra("app", path).putExtra("name", name))
                }
                setOnLongClickListener {
                    val where = py.callAttr("data_path", path).toString()
                    AlertDialog.Builder(this@MainActivity).setTitle(name)
                        .setItems(arrayOf("Clear game data", "Remove game")) { _, which ->
                            if (which == 0) AlertDialog.Builder(this@MainActivity)
                                .setTitle("Clear $name's data?")
                                .setMessage("Deletes its saves and settings, as reinstalling it did on an iPhone. The game stays installed.\n\n$where")
                                .setPositiveButton("Clear") { _, _ -> py.callAttr("clear_data", path); toast("$name's data cleared.") }
                                .setNegativeButton("Cancel", null).show()
                            else AlertDialog.Builder(this@MainActivity).setTitle("Remove $name?")
                                .setMessage("Its saves and settings are kept.")
                                .setPositiveButton("Remove") { _, _ -> py.callAttr("remove_app", path); refresh() }
                                .setNegativeButton("Cancel", null).show()
                        }.show()
                    true
                }
            })
            list.addView(View(this).apply { setBackgroundColor(Color.DKGRAY) }, LinearLayout.LayoutParams(-1, 1))
        }
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
