/// Reloads the browser tab, so a page left open from before a deploy stops running against a hub that has already moved on.
library;

import 'package:web/web.dart' as web;

void reloadPage() => web.window.location.reload();
