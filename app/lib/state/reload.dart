/// Reloads the current page, where there is one to reload.
///
/// Isolated in its own conditionally-compiled file rather than used
/// directly -- `dart:html` only exists on the web target, and the app's
/// other platform-facing code (`api/client.dart`) deliberately avoids it so
/// it stays compilable for the mobile targets too. This file gives the
/// stale-page banner in `main.dart` the same guarantee without spreading
/// `dart:html` into files that do not need it.
library;

export 'reload_stub.dart' if (dart.library.html) 'reload_web.dart';
