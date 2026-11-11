package com.tunombre.localizadorsms

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothSocket
import android.content.Context
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.File
import java.io.IOException
import java.io.InputStreamReader
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

// ====================================================================
//  P3-S3 · SENSORES DE LA CARGA (ESP32 por Bluetooth)
//  El ESP32 de sensores va en la zona de la carga y NO tiene Wi-Fi:
//  habla por Bluetooth SPP con este celular. Durante un viaje con envío,
//  TransmisionService abre la conexión, le manda INICIO;ENVIO:<n>, lee
//  lo que mande (eventos, luz y latidos), le agrega el último GPS y la
//  hora, y lo sube a POST /api/camiones/viaje/eventos.
//  Si no hay señal, los eventos esperan en una cola guardada en el
//  celular (sobrevive a cerrar la app) y se reintentan.
//  Si no hay ESP elegido o no responde, el viaje sigue igual (S2).
//
//  Protocolo (una línea por mensaje):
//    celular -> ESP:  PING | INICIO;ENVIO:<id> | FIN
//    ESP -> celular:  PONG | HOLA;DISP:SENSORES;VER:1
//                     EVENTO;TIPO:FRENADA|HUECO|CURVA;G:0.62;SEQ:<n>
//                     LUZ;ESTADO:ABIERTA|CERRADA;NIVEL:812;SEQ:<n>
//                     LATIDO;LUZ:<nivel>;ESTADO:<ABIERTA|CERRADA>   (cada 10 s)
// ====================================================================

/** Lo que la pantalla muestra de los sensores (lo escribe la conexión, en el hilo principal). */
object EstadoSensores {
    var activo by mutableStateOf(false)                   // hay un viaje intentando hablar con el ESP
    var conectado by mutableStateOf(false)
    var ultimoLatidoMs by mutableLongStateOf(0L)          // System.currentTimeMillis() del último mensaje del ESP
    var luzAbierta by mutableStateOf<Boolean?>(null)      // null = todavía no se sabe
    var nivelLuz by mutableStateOf<Int?>(null)
    var frenadas by mutableIntStateOf(0)                  // contados en este celular durante el viaje
    var huecos by mutableIntStateOf(0)
    var curvas by mutableIntStateOf(0)
    var pendientes by mutableIntStateOf(0)                // eventos en cola sin subir al servidor
    var mensaje by mutableStateOf("")
    var envioId by mutableStateOf<Int?>(null)

    // Última posición GPS del viaje (la pone TransmisionService). Se lee desde otros hilos.
    @Volatile var ultimaPosicion: Pair<Double, Double>? = null
}

// ====================================================================
//  Equipo elegido (se guarda en el celular, como el de reconocimiento)
// ====================================================================
object Sensores {
    private const val PREFS = "rastreo_prefs"
    private const val PREF_DIRECCION = "sensores_direccion"
    private const val PREF_NOMBRE = "sensores_nombre"

    fun guardado(contexto: Context): EquipoBluetooth? {
        val prefs = contexto.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val direccion = prefs.getString(PREF_DIRECCION, null) ?: return null
        return EquipoBluetooth(prefs.getString(PREF_NOMBRE, null) ?: direccion, direccion)
    }

    fun guardar(contexto: Context, equipo: EquipoBluetooth?) {
        val editor = contexto.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
        if (equipo == null) {
            editor.remove(PREF_DIRECCION).remove(PREF_NOMBRE)
        } else {
            editor.putString(PREF_DIRECCION, equipo.direccion).putString(PREF_NOMBRE, equipo.nombre)
        }
        editor.apply()
    }
}

// ====================================================================
//  COLA PERSISTENTE (un archivo JSON en la memoria interna de la app)
//  Cada evento lleva su "uid": si se reenvía, el servidor no lo duplica.
// ====================================================================
object ColaEventos {
    private const val ARCHIVO = "cola_eventos_carga.json"
    private const val MAXIMO = 3000                        // si se llena (días sin señal), se descartan los más viejos
    private const val LOTE = 50
    private val candado = Any()
    private val enviando = Mutex()                         // un envío al servidor a la vez

    private fun archivo(contexto: Context) = File(contexto.filesDir, ARCHIVO)

    private fun leer(contexto: Context): JSONArray = try {
        val f = archivo(contexto)
        if (f.exists()) JSONArray(f.readText(Charsets.UTF_8)) else JSONArray()
    } catch (e: Exception) {
        JSONArray()                                        // archivo dañado: se empieza de cero
    }

    private fun escribir(contexto: Context, lista: JSONArray) {
        val f = archivo(contexto)
        val temporal = File(f.parentFile, "$ARCHIVO.tmp")
        temporal.writeText(lista.toString(), Charsets.UTF_8)
        if (!temporal.renameTo(f)) {                       // en Android renameTo reemplaza; por si acaso
            f.delete()
            temporal.renameTo(f)
        }
    }

    fun cantidad(contexto: Context): Int = synchronized(candado) { leer(contexto).length() }

    fun agregar(contexto: Context, evento: JSONObject) {
        synchronized(candado) {
            val lista = leer(contexto)
            lista.put(evento)
            val recortada = if (lista.length() > MAXIMO) {
                JSONArray().also { n -> for (i in lista.length() - MAXIMO until lista.length()) n.put(lista.get(i)) }
            } else lista
            escribir(contexto, recortada)
            EstadoSensores.pendientes = recortada.length()
        }
    }

    private fun quitar(contexto: Context, uids: Set<String>) {
        if (uids.isEmpty()) return
        synchronized(candado) {
            val lista = leer(contexto)
            val nueva = JSONArray()
            for (i in 0 until lista.length()) {
                val e = lista.optJSONObject(i) ?: continue
                if (e.optString("uid") !in uids) nueva.put(e)
            }
            escribir(contexto, nueva)
            EstadoSensores.pendientes = nueva.length()
        }
    }

    /**
     * Sube lo que haya en la cola (por lotes, agrupado por envío) y, si viene, el latido del envío actual.
     * Devuelve true si no quedó nada pendiente por un error de red o del servidor.
     */
    suspend fun vaciar(
        contexto: Context,
        latido: JSONObject? = null,
        idEnvioLatido: Int? = null,
        posicion: Pair<Double, Double>? = null
    ): Boolean = enviando.withLock {
        val token = Sesion.token(contexto) ?: return@withLock false
        val porEnvio = LinkedHashMap<Int, MutableList<JSONObject>>()
        synchronized(candado) {
            val lista = leer(contexto)
            EstadoSensores.pendientes = lista.length()
            for (i in 0 until lista.length()) {
                val e = lista.optJSONObject(i) ?: continue
                val id = e.optInt("id_envio", 0)
                if (id > 0) porEnvio.getOrPut(id) { mutableListOf() }.add(e)
            }
        }
        if (idEnvioLatido != null && latido != null) porEnvio.getOrPut(idEnvioLatido) { mutableListOf() }
        var todoBien = true
        for ((idEnvio, eventos) in porEnvio) {
            var resto: List<JSONObject> = eventos
            var primero = true
            while (primero || resto.isNotEmpty()) {
                primero = false
                val lote = resto.take(LOTE)
                resto = resto.drop(LOTE)
                val cuerpo = JSONObject()
                    .put("id_envio", idEnvio)
                    .put("eventos", JSONArray().also { a -> lote.forEach { e -> a.put(sinIdEnvio(e)) } })
                if (idEnvio == idEnvioLatido && latido != null) cuerpo.put("latido", latido)
                if (posicion != null) cuerpo.put("posicion", JSONObject().put("lat", posicion.first).put("lon", posicion.second))
                if (lote.isEmpty() && !cuerpo.has("latido")) break
                when (val r = ApiOrbita.eventos(token, cuerpo)) {
                    is Respuesta.Ok -> {
                        val listos = HashSet<String>()
                        listOf("guardados", "repetidos").forEach { clave ->
                            r.json.optJSONArray(clave)?.let { a -> for (i in 0 until a.length()) listos.add(a.optString(i)) }
                        }
                        // Los que el servidor rechazó por estar mal formados no se reintentan (fallarían siempre).
                        r.json.optJSONArray("rechazados")?.let { a ->
                            for (i in 0 until a.length()) a.optJSONObject(i)?.texto("uid")?.let { listos.add(it) }
                        }
                        quitar(contexto, listos)
                    }
                    is Respuesta.Error -> {
                        if ((r.codigo == 404 && r.campo == "id_envio") || r.codigo == 400 || r.codigo == 413) {
                            // Ese envío no existe / no es de este camión, o el lote es inválido: no tiene arreglo.
                            quitar(contexto, lote.map { it.optString("uid") }.toSet())
                        } else {
                            todoBien = false                       // sin señal, sesión vencida o servidor viejo: se reintenta
                            withContext(Dispatchers.Main) {
                                EstadoSensores.mensaje = if (r.codigo == 0) "Sin señal: los eventos esperan en el celular"
                                else "El servidor no recibió los eventos: ${r.mensaje}"
                            }
                            return@withLock false
                        }
                    }
                }
            }
        }
        todoBien
    }

    private fun sinIdEnvio(e: JSONObject): JSONObject {
        val copia = JSONObject(e.toString())
        copia.remove("id_envio")
        return copia
    }
}

// ====================================================================
//  CONEXIÓN CON EL ESP DURANTE UN VIAJE (la corre TransmisionService)
// ====================================================================
class ConexionSensores(
    private val contexto: Context,
    private val direccion: String,
    private val idEnvio: Int
) {
    companion object {
        private const val REINTENTO_MS = 5_000L            // si se cae, se vuelve a conectar
        private const val SILENCIO_MAXIMO_MS = 35_000L     // el ESP late cada 10 s: 35 s sin nada = conexión muerta
        private const val ENVIO_MS = 3_000L                // cada cuánto se sube la cola
    }

    @Volatile private var socket: BluetoothSocket? = null
    @Volatile private var ultimaLineaMs = 0L
    @Volatile private var latidoPendiente: JSONObject? = null
    private val escritura = Any()
    private val formatoHora = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssXXX", Locale.US)

    /** Corre hasta que la cancelen (al detener la transmisión). Nunca lanza errores. */
    suspend fun correr() = withContext(Dispatchers.IO) {
        enPrincipal {
            if (EstadoSensores.envioId != idEnvio) {          // viaje nuevo: los contadores empiezan en cero
                EstadoSensores.frenadas = 0
                EstadoSensores.huecos = 0
                EstadoSensores.curvas = 0
                EstadoSensores.luzAbierta = null
                EstadoSensores.nivelLuz = null
                EstadoSensores.ultimoLatidoMs = 0L
            }
            EstadoSensores.envioId = idEnvio
            EstadoSensores.activo = true
            EstadoSensores.conectado = false
            EstadoSensores.mensaje = "Conectando con los sensores…"
            EstadoSensores.pendientes = ColaEventos.cantidad(contexto)
        }
        // Al cancelar: avisar FIN al ESP y cerrar (eso destraba la lectura bloqueada).
        launch {
            try {
                awaitCancellation()
            } finally {
                terminar()
                withContext(kotlinx.coroutines.NonCancellable + Dispatchers.Main) {
                    EstadoSensores.activo = false
                    EstadoSensores.conectado = false
                    EstadoSensores.mensaje = ""
                }
            }
        }
        // Subir la cola y el latido al servidor.
        launch {
            while (isActive) {
                val latido = latidoPendiente
                latidoPendiente = null
                val ok = try {
                    ColaEventos.vaciar(contexto, latido, idEnvio, EstadoSensores.ultimaPosicion)
                } catch (e: CancellationException) {
                    throw e
                } catch (e: Exception) {
                    false
                }
                if (!ok && latido != null && latidoPendiente == null) latidoPendiente = latido
                delay(if (ok) ENVIO_MS else 10_000L)
            }
        }
        // Vigilante: si el ESP deja de hablar, se corta para reconectar.
        launch {
            while (isActive) {
                delay(5_000L)
                if (socket != null && ultimaLineaMs > 0 && System.currentTimeMillis() - ultimaLineaMs > SILENCIO_MAXIMO_MS) {
                    cerrar(socket)
                }
            }
        }
        while (isActive) {
            try {
                sesion()
            } catch (e: CancellationException) {
                throw e
            } catch (e: SecurityException) {
                enPrincipal { EstadoSensores.mensaje = "Falta el permiso de Bluetooth para los sensores" }
            } catch (e: IOException) {
                // se cayó o no se pudo conectar: se reintenta
            } catch (e: Exception) {
                enPrincipal { EstadoSensores.mensaje = "Error con los sensores: ${e.message}" }
            }
            socket = null
            if (!isActive) break
            enPrincipal {
                if (EstadoSensores.conectado) EstadoSensores.mensaje = "Se perdió la conexión con los sensores: reconectando…"
                else if (!EstadoSensores.mensaje.startsWith("Falta")) EstadoSensores.mensaje = "Los sensores no responden: reintentando…"
                EstadoSensores.conectado = false
            }
            delay(REINTENTO_MS)
        }
    }

    @SuppressLint("MissingPermission")
    private suspend fun sesion() {
        if (!VerificacionConductor.tienePermiso(contexto)) throw SecurityException("sin permiso")
        val adaptador = VerificacionConductor.adaptador(contexto) ?: throw IOException("sin Bluetooth")
        if (!adaptador.isEnabled) {
            enPrincipal { EstadoSensores.mensaje = "El Bluetooth está apagado: los sensores no se pueden conectar" }
            throw IOException("Bluetooth apagado")
        }
        val equipo = try {
            adaptador.getRemoteDevice(direccion)
        } catch (e: IllegalArgumentException) {
            throw IOException("dirección inválida")
        }
        val s = conectar(equipo)
        val entrada = BufferedReader(InputStreamReader(s.inputStream, Charsets.UTF_8))
        ultimaLineaMs = System.currentTimeMillis()
        escribir("PING")
        escribir("INICIO;ENVIO:$idEnvio")
        enPrincipal {
            EstadoSensores.conectado = true
            EstadoSensores.mensaje = "Sensores conectados"
        }
        var linea = entrada.readLine()
        while (linea != null) {
            ultimaLineaMs = System.currentTimeMillis()
            atender(linea)
            linea = entrada.readLine()
        }
        throw IOException("el ESP cerró la conexión")
    }

    @SuppressLint("MissingPermission")
    private fun conectar(equipo: BluetoothDevice): BluetoothSocket {
        VerificacionConductor.adaptador(contexto)?.let { a -> try { a.cancelDiscovery() } catch (e: SecurityException) {} }
        val normal = equipo.createRfcommSocketToServiceRecord(VerificacionConductor.UUID_SPP)
        socket = normal
        try {
            normal.connect()
            return normal
        } catch (e: IOException) {
            cerrar(normal)
            if (socket == null) throw e                     // se canceló mientras conectaba
        }
        // Segundo intento sin cifrado obligatorio (algunos PC con Windows y módulos lo necesitan).
        val insegura = equipo.createInsecureRfcommSocketToServiceRecord(VerificacionConductor.UUID_SPP)
        socket = insegura
        insegura.connect()
        return insegura
    }

    private fun escribir(texto: String) {
        val s = socket ?: throw IOException("sin conexión")
        synchronized(escritura) {
            s.outputStream.write("$texto\n".toByteArray(Charsets.UTF_8))
            s.outputStream.flush()
        }
    }

    private fun terminar() {
        val s = socket
        socket = null
        if (s != null) {
            try {
                synchronized(escritura) {
                    s.outputStream.write("FIN\n".toByteArray(Charsets.UTF_8))
                    s.outputStream.flush()
                }
            } catch (e: Exception) {
                // ya estaba desconectado
            }
        }
        cerrar(s)
    }

    private fun cerrar(s: BluetoothSocket?) {
        try {
            s?.close()
        } catch (e: IOException) {
            // ya estaba cerrado
        }
    }

    // --- mensajes del ESP ---
    private suspend fun atender(linea: String) {
        val (tipo, campos) = interpretar(linea)
        when (tipo) {
            "EVENTO" -> {
                val nombre = when (campos["TIPO"]?.uppercase(Locale.ROOT)) {
                    "FRENADA" -> "frenada"
                    "HUECO" -> "hueco"
                    "CURVA" -> "curva"
                    else -> return
                }
                val g = campos["G"]?.toDoubleOrNull()?.takeIf { it in 0.0..16.0 }
                guardarEvento(nombre, g)
                enPrincipal {
                    when (nombre) {
                        "frenada" -> EstadoSensores.frenadas += 1
                        "hueco" -> EstadoSensores.huecos += 1
                        else -> EstadoSensores.curvas += 1
                    }
                    EstadoSensores.ultimoLatidoMs = System.currentTimeMillis()
                }
            }
            "LUZ" -> {
                val abierta = when (campos["ESTADO"]?.uppercase(Locale.ROOT)) {
                    "ABIERTA" -> true
                    "CERRADA" -> false
                    else -> return
                }
                val nivel = campos["NIVEL"]?.toIntOrNull()?.takeIf { it in 0..4095 }
                // Solo se sube cuando cambia (el ESP manda el estado actual también al recibir INICIO).
                // Al conectar (estado desconocido), una carga cerrada no es noticia; una abierta sí.
                val antes = EstadoSensores.luzAbierta
                if (antes != abierta && (antes != null || abierta)) {
                    guardarEvento(if (abierta) "luz_abierta" else "luz_cerrada", nivel?.toDouble())
                }
                latidoPendiente = JSONObject().put("luz", if (abierta) "abierta" else "cerrada")
                    .put("nivel", nivel ?: JSONObject.NULL).put("momento", ahoraIso())
                enPrincipal {
                    EstadoSensores.luzAbierta = abierta
                    EstadoSensores.nivelLuz = nivel
                    EstadoSensores.ultimoLatidoMs = System.currentTimeMillis()
                }
            }
            "LATIDO" -> {
                val nivel = campos["LUZ"]?.toIntOrNull()?.takeIf { it in 0..4095 }
                val estado = campos["ESTADO"]?.uppercase(Locale.ROOT)
                val abierta = when (estado) { "ABIERTA" -> true; "CERRADA" -> false; else -> null }
                latidoPendiente = JSONObject()
                    .put("luz", when (abierta) { true -> "abierta"; false -> "cerrada"; null -> JSONObject.NULL })
                    .put("nivel", nivel ?: JSONObject.NULL)
                    .put("momento", ahoraIso())
                enPrincipal {
                    if (abierta != null) EstadoSensores.luzAbierta = abierta
                    if (nivel != null) EstadoSensores.nivelLuz = nivel
                    EstadoSensores.ultimoLatidoMs = System.currentTimeMillis()
                    EstadoSensores.conectado = true
                    EstadoSensores.mensaje = "Sensores conectados"
                }
            }
            "HOLA", "PONG" -> enPrincipal { EstadoSensores.ultimoLatidoMs = System.currentTimeMillis() }
            // Otros mensajes se ignoran (versiones futuras del firmware).
        }
    }

    private fun guardarEvento(tipo: String, valor: Double?) {
        val posicion = EstadoSensores.ultimaPosicion
        val evento = JSONObject()
            .put("id_envio", idEnvio)
            .put("uid", "c" + UUID.randomUUID().toString().replace("-", ""))   // único: el servidor no lo duplica
            .put("tipo", tipo)
            .put("valor", valor ?: JSONObject.NULL)
            .put("momento", ahoraIso())
        if (posicion != null) evento.put("lat", posicion.first).put("lon", posicion.second)
        try {
            ColaEventos.agregar(contexto, evento)
        } catch (e: Exception) {
            // memoria llena o similar: se pierde este evento, pero el viaje sigue
        }
    }

    private fun ahoraIso(): String = synchronized(formatoHora) { formatoHora.format(Date()) }

    private fun interpretar(linea: String): Pair<String, Map<String, String>> {
        val trozos = linea.trim().split(";")
        val tipo = trozos.first().trim().uppercase(Locale.ROOT)
        val campos = HashMap<String, String>()
        for (trozo in trozos.drop(1)) {
            val i = trozo.indexOf(':')
            if (i > 0) campos[trozo.substring(0, i).trim().uppercase(Locale.ROOT)] = trozo.substring(i + 1).trim()
        }
        return tipo to campos
    }

    private suspend fun enPrincipal(bloque: () -> Unit) = withContext(Dispatchers.Main) { bloque() }
}

/** "hace 4 s", "hace 2 min" (para el último latido). */
internal fun textoHace(ms: Long, ahora: Long = System.currentTimeMillis()): String {
    if (ms <= 0L) return "nunca"
    val s = ((ahora - ms) / 1000).coerceAtLeast(0)
    return when {
        s < 60 -> "hace $s s"
        s < 3600 -> "hace ${s / 60} min"
        else -> "hace ${s / 3600} h"
    }
}
