package com.tunombre.localizadorsms

import android.annotation.SuppressLint
import android.content.Context
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.viewinterop.AndroidView

// ====================================================================
//  P3-S2 · MAPA DEL VIAJE (Leaflet dentro de un WebView)
//  Es el mismo mapa de la página: ruta planeada, puntos de control
//  coloreados según el retraso, alertas y el camión en vivo.
//  El HTML está en assets/mapa_viaje.html; Leaflet va en assets/ para
//  no depender de un servidor externo para la librería (las teselas del
//  mapa sí vienen de internet).
// ====================================================================

private object HtmlDelMapa {
    @Volatile private var cache: String? = null

    fun leer(contexto: Context): String = cache ?: run {
        fun asset(nombre: String) = contexto.assets.open(nombre).bufferedReader(Charsets.UTF_8).use { it.readText() }
        asset("mapa_viaje.html")
            .replace("/*LEAFLET_CSS*/", asset("leaflet.css"))
            .replace("/*LEAFLET_JS*/", asset("leaflet.js"))
            .also { cache = it }
    }
}

/**
 * rutaJson    {"puntos": [[lat, lon]...], "dist_m": [...]} (la línea planeada; llega una vez por viaje)
 * viajeJson   el viaje tal como lo manda el servidor (puntos de control, avance, alertas)
 * camion      posición del celular
 * seguirCamion true = el mapa sigue al camión; false = muestra toda la ruta
 */
@SuppressLint("SetJavaScriptEnabled")
@Composable
fun MapaViaje(
    rutaJson: String?,
    viajeJson: String?,
    camion: Pair<Double, Double>?,
    seguirCamion: Boolean,
    modifier: Modifier = Modifier
) {
    val contexto = LocalContext.current
    var listo by remember { mutableStateOf(false) }
    val webView = remember {
        WebView(contexto).apply {
            // IMPORTANTE: AndroidView deja el WebView con alto WRAP_CONTENT y, en ese modo, Chromium
            // calcula los "height: 100%" de la página como 0 px: el mapa quedaba de alto 0 (rectángulo gris).
            // Con MATCH_PARENT la página toma el alto que le da Compose (240/270 dp).
            layoutParams = android.view.ViewGroup.LayoutParams(
                android.view.ViewGroup.LayoutParams.MATCH_PARENT,
                android.view.ViewGroup.LayoutParams.MATCH_PARENT
            )
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            setBackgroundColor(0xFFDDE3EA.toInt())
            val depurar = (contexto.applicationInfo.flags and android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0
            if (depurar) {
                WebView.setWebContentsDebuggingEnabled(true)
                webChromeClient = object : android.webkit.WebChromeClient() {
                    override fun onConsoleMessage(m: android.webkit.ConsoleMessage): Boolean {
                        android.util.Log.d("MapaViaje", "${m.messageLevel()} ${m.sourceId()}:${m.lineNumber()} ${m.message()}")
                        return true
                    }
                }
            }
            webViewClient = object : WebViewClient() {
                override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                    if (depurar) android.util.Log.d("MapaViaje", "onPageStarted $url")
                }
                override fun onPageFinished(view: WebView?, url: String?) {
                    if (depurar) android.util.Log.d("MapaViaje", "onPageFinished $url tam=${view?.width}x${view?.height}")
                    listo = true
                }
                override fun onReceivedError(view: WebView?, req: android.webkit.WebResourceRequest?, err: android.webkit.WebResourceError?) {
                    if (depurar) android.util.Log.e("MapaViaje", "error ${req?.url} ${err?.errorCode} ${err?.description}")
                }
                override fun onReceivedHttpError(view: WebView?, req: android.webkit.WebResourceRequest?, res: android.webkit.WebResourceResponse?) {
                    if (depurar) android.util.Log.e("MapaViaje", "http ${res?.statusCode} ${req?.url}")
                }
            }
            // Con una dirección https como base, las teselas de OpenStreetMap reciben un "Referer"
            // válido (desde un archivo local no lo tendrían y OSM las bloquea).
            loadDataWithBaseURL(Config.URL_API + "/", HtmlDelMapa.leer(contexto), "text/html", "utf-8", null)
        }
    }
    // Al salir de la pantalla se libera el WebView (memoria del motor del navegador).
    AndroidView(factory = { webView }, modifier = modifier, onRelease = { it.destroy() })

    LaunchedEffect(listo, seguirCamion) {
        if (listo) webView.evaluateJavascript("orbita.seguir($seguirCamion)", null)
    }
    LaunchedEffect(listo, rutaJson) {
        if (listo && rutaJson != null) webView.evaluateJavascript("orbita.ruta(${paraJs(rutaJson)})", null)
    }
    LaunchedEffect(listo, viajeJson) {
        if (listo && viajeJson != null) webView.evaluateJavascript("orbita.viaje(${paraJs(viajeJson)})", null)
    }
    LaunchedEffect(listo, camion) {
        if (listo && camion != null) {
            webView.evaluateJavascript("orbita.camion(${camion.first}, ${camion.second})", null)
        }
    }
}

// El JSON ya es JavaScript válido, salvo dos caracteres raros que JavaScript no acepta dentro de un texto.
private fun paraJs(json: String): String = json.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
