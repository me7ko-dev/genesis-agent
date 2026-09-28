package expo.modules.termuxbridge

import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition

// Genesis on the phone itself: the agent lives in Termux (scripts/install-termux.sh),
// this app is its window. Here the app asks Termux to run a command — the
// Android side of Genesis Desktop starting `genesis serve` on the computer.
// Protocol: https://github.com/termux/termux-app/wiki/RUN_COMMAND-Intent
class TermuxBridgeModule : Module() {
  private val context
    get() = requireNotNull(appContext.reactContext) { "React context is not ready" }

  override fun definition() = ModuleDefinition {
    Name("TermuxBridge")

    Function("isTermuxInstalled") {
      try {
        context.packageManager.getPackageInfo(TERMUX, 0)
        true
      } catch (e: PackageManager.NameNotFoundException) {
        false
      }
    }

    Function("hasRunPermission") {
      context.checkSelfPermission(RUN_PERMISSION) == PackageManager.PERMISSION_GRANTED
    }

    // background = true: no terminal window, the command just runs.
    // background = false: Termux comes to the front and shows it.
    // Throws SecurityException without the permission (the JS side asks first).
    Function("runCommand") { path: String, args: List<String>, workdir: String, background: Boolean ->
      val intent = Intent().apply {
        setClassName(TERMUX, "$TERMUX.app.RunCommandService")
        action = "$TERMUX.RUN_COMMAND"
        putExtra("$TERMUX.RUN_COMMAND_PATH", path)
        putExtra("$TERMUX.RUN_COMMAND_ARGUMENTS", args.toTypedArray())
        putExtra("$TERMUX.RUN_COMMAND_WORKDIR", workdir)
        putExtra("$TERMUX.RUN_COMMAND_BACKGROUND", background)
        // "0": open a new session and switch Termux to it.
        putExtra("$TERMUX.RUN_COMMAND_SESSION_ACTION", "0")
      }
      val started = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        context.startForegroundService(intent)
      } else {
        context.startService(intent)
      }
      started != null
    }

    Function("openTermux") {
      val launch = context.packageManager.getLaunchIntentForPackage(TERMUX)
      if (launch == null) {
        false
      } else {
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(launch)
        true
      }
    }
  }

  companion object {
    const val TERMUX = "com.termux"
    const val RUN_PERMISSION = "com.termux.permission.RUN_COMMAND"
  }
}
