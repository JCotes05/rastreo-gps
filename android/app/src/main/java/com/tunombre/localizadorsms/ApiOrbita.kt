package com.tunombre.localizadorsms

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import androidx.core.content.ContextCompat
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import com.google.android.gms.tasks.CancellationTokenSource
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import kotlin.coroutines.resume

// ====================================================================
//  P3-S2 · LA APP HABLA CON LA API DEL SERVIDOR (/api/camiones/...)
//  - HTTPS + JSON, sin librerías nuevas (HttpURLConnection y org.json).
//  - Cada camión inicia sesión con su placa y contraseña; el servidor le
//    da un "token" que se guarda en el celular y viaja en cada pedido
//    (encabezado Authorization: Bearer ...). Dura 30 días.
//  - La ubicación del viaje NO va por aquí: sigue yendo por UDP (S1),
//    ahora con los campos ENVIO y VEL.
// ====================================================================

/** Respuesta de la API: el JSON si salió bien, o el mensaje de error del servidor. */
sealed class Respuesta {
    data class Ok(val json: JSONObject) : Respuesta()

    /** codigo 0 = no hubo conexión. campo = el campo del formulario que falló (si el servidor lo dice). */
    data class Error(val codigo: Int, val mensaje: String, val campo: String? = null) : Respuesta()
}

object ApiOrbita {

    private const val SIN_API = "El servidor todavía no tiene la API de envíos (paso 3 del P3-S2)."

    suspend fun registrar(datos: JSONObject): Respuesta = pedir("POST", "/api/camiones/registro", datos)

    suspend fun ingresar(placa: String, clave: String): Respuesta =
        pedir("POST", "/api/camiones/login", JSONObject().put("placa", placa).put("clave", clave))

    suspend fun salir(token: String): Respuesta = pedir("POST", "/api/camiones/logout", JSONObject(), token)

    suspend fun disponibilidad(token: String, disponible: Boolean, lat: Double?, lon: Double?): Respuesta {
        val cuerpo = JSONObject().put("disponible", disponible)
        if (lat != null && lon != null) cuerpo.put("lat", lat).put("lon", lon)
        return pedir("POST", "/api/camiones/disponibilidad", cuerpo, token)
    }

    suspend fun ubicacion(token: String, lat: Double, lon: Double): Respuesta =
        pedir("POST", "/api/camiones/ubicacion", JSONObject().put("lat", lat).put("lon", lon), token)

    /** Viaje asignado o en curso (o null). conRuta = trae también la línea planeada. */
    suspend fun viaje(token: String, conRuta: Boolean): Respuesta {
        val r = pedir("GET", "/api/camiones/viaje" + (if (conRuta) "?ruta=1" else ""), null, token)
        // Un servidor sin el paso 3 contesta /datos a cualquier ruta: se reconoce porque no trae "viaje".
        if (r is Respuesta.Ok && !r.json.has("viaje")) return Respuesta.Error(404, SIN_API)
        return r
    }

    suspend fun finalizar(token: String): Respuesta =
        pedir("POST", "/api/camiones/viaje/finalizar", JSONObject(), token)

    // ---------------------------------------------------------------
    //  Un pedido HTTP (en segundo plano; la red no se usa en el hilo principal)
    // ---------------------------------------------------------------
    private suspend fun pedir(metodo: String, ruta: String, cuerpo: JSONObject?, token: String? = null): Respuesta =
        withContext(Dispatchers.IO) {
            var conexion: HttpURLConnection? = null
            try {
                conexion = URL(Config.URL_API + ruta).openConnection() as HttpURLConnection
                conexion.requestMethod = metodo
                conexion.connectTimeout = 10_000
                conexion.readTimeout = 30_000          // registrar e ingresar tardan ~1 s (la contraseña se protege con PBKDF2)
                conexion.setRequestProperty("Accept", "application/json")
                if (token != null) conexion.setRequestProperty("Authorization", "Bearer $token")
                if (cuerpo != null) {
                    val bytes = cuerpo.toString().toByteArray(Charsets.UTF_8)
                    conexion.doOutput = true
                    conexion.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                    conexion.setFixedLengthStreamingMode(bytes.size)
                    conexion.outputStream.use { it.write(bytes) }
                }
                val codigo = conexion.responseCode
                val flujo = if (codigo >= 400) conexion.errorStream else conexion.inputStream
                val texto = flujo?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }.orEmpty()
                val json = try {
                    if (texto.isBlank()) JSONObject() else JSONObject(texto)
                } catch (e: JSONException) {
                    null                                 // no es JSON: página de error o servidor sin la API
                }
                when {
                    json == null && (codigo == 404 || codigo == 405 || codigo == 501 || codigo < 400) ->
                        Respuesta.Error(codigo, SIN_API)
                    json == null -> Respuesta.Error(codigo, "Error $codigo del servidor.")
                    codigo in 200..299 -> Respuesta.Ok(json)
                    else -> Respuesta.Error(
                        codigo,
                        json.texto("error") ?: "Error $codigo del servidor.",
                        json.texto("campo")
                    )
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Respuesta.Error(0, "Sin conexión con el servidor. Revisa los datos móviles o el Wi-Fi.")
            } finally {
                conexion?.disconnect()
            }
        }
}

// ====================================================================
//  SESIÓN DEL CAMIÓN (se guarda en el celular)
// ====================================================================
object Sesion {
    private const val PREFS = "rastreo_prefs"
    private const val PREF_TOKEN = "token_camion"
    private const val PREF_CAMION = "camion_json"

    fun token(contexto: Context): String? =
        contexto.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(PREF_TOKEN, null)

    fun camionGuardado(contexto: Context): Camion? {
        val texto = contexto.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(PREF_CAMION, null) ?: return null
        return try {
            Camion.desde(JSONObject(texto))
        } catch (e: JSONException) {
            null
        }
    }

    fun guardar(contexto: Context, token: String, camion: JSONObject) {
        contexto.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString(PREF_TOKEN, token)
            .putString(PREF_CAMION, camion.toString())
            .apply()
    }

    fun guardarCamion(contexto: Context, camion: JSONObject) {
        contexto.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString(PREF_CAMION, camion.toString())
            .apply()
    }

    fun borrar(contexto: Context) {
        contexto.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .remove(PREF_TOKEN)
            .remove(PREF_CAMION)
            .apply()
    }
}

// ====================================================================
//  DATOS QUE MUESTRA LA APP (se leen del JSON del servidor)
// ====================================================================

// En org.json, optString() de un null devuelve el texto "null": por eso estas ayudas.
internal fun JSONObject.texto(nombre: String): String? =
    if (isNull(nombre)) null else optString(nombre).takeIf { it.isNotBlank() }

internal fun JSONObject.entero(nombre: String): Int? = if (isNull(nombre)) null else optInt(nombre)

internal fun JSONObject.decimal(nombre: String): Double? =
    if (isNull(nombre)) null else optDouble(nombre).takeIf { !it.isNaN() }

private fun JSONArray?.objetos(): List<JSONObject> {
    if (this == null) return emptyList()
    return (0 until length()).mapNotNull { optJSONObject(it) }
}

data class Camion(
    val placa: String,
    val configuracion: String,
    val configuracionNombre: String,
    val carroceriaNombre: String,
    val capacidadKg: Int,
    val marca: String,
    val linea: String,
    val modelo: Int,
    val color: String,
    val estado: String,                 // no_disponible | disponible | asignado | en_ruta
    val ubicacionHaceMin: Int?,
    val soatVence: String,
    val tecnomecanicaVence: String,
    val documentosVigentes: Boolean,
    val propietario: String?
) {
    companion object {
        fun desde(j: JSONObject): Camion {
            val documentos = j.optJSONObject("documentos")
            return Camion(
                placa = j.texto("placa") ?: "",
                configuracion = j.texto("configuracion") ?: "",
                configuracionNombre = j.texto("configuracion_nombre") ?: "",
                carroceriaNombre = j.texto("carroceria_nombre") ?: "",
                capacidadKg = j.entero("capacidad_kg") ?: 0,
                marca = j.texto("marca") ?: "",
                linea = j.texto("linea") ?: "",
                modelo = j.entero("modelo") ?: 0,
                color = j.texto("color") ?: "",
                estado = j.texto("estado") ?: "no_disponible",
                ubicacionHaceMin = j.optJSONObject("ubicacion")?.entero("hace_min"),
                soatVence = documentos?.texto("soat_vence") ?: "",
                tecnomecanicaVence = documentos?.texto("tecnomecanica_vence") ?: "",
                documentosVigentes = documentos?.optBoolean("vigentes", true) ?: true,
                propietario = j.optJSONObject("propietario")?.texto("nombre")
            )
        }
    }
}

data class PuntoControl(
    val orden: Int,
    val tipo: String,                   // salida | control | llegada
    val nombre: String,
    val distanciaM: Double,
    val horaPlan: String?,
    val horaReal: String?,
    val retrasoS: Int?,
    val nivel: String                   // pendiente | a_tiempo | aviso | alerta
)

data class AlertaViaje(val id: Int, val tipo: String, val activa: Boolean, val valorMax: Double?)

data class Viaje(
    val id: Int,
    val codigo: String,
    val estado: String,                 // asignado | en_ruta | en_destino
    val cliente: String,
    val origenDireccion: String,
    val origenContacto: String?,
    val origenCelular: String?,
    val destinoDireccion: String,
    val destinatario: String?,
    val destinoCelular: String?,
    val cargaTipo: String,
    val descripcion: String,
    val empaque: String,
    val cantidad: Int?,
    val pesoKg: Int,
    val cargueProgramado: String?,
    val distanciaM: Double,
    val duracionS: Int,
    val idRecorrido: String?,
    val salidaReal: String?,
    val avanceM: Int,
    val porcentaje: Int,
    val recorridoM: Int,
    val retrasoS: Int?,
    val nivelRetraso: String?,
    val desvioM: Int?,
    val velocidadKmh: Int?,
    val llegadaEstimada: String?,
    val puntos: List<PuntoControl>,
    val alertas: List<AlertaViaje>,
    val conductor: String?,
    val json: JSONObject                // tal como llegó (el mapa lo usa entero)
) {
    val desvioActivo: AlertaViaje? get() = alertas.firstOrNull { it.activa && it.tipo == "desvio" }
    val retrasoActivo: AlertaViaje? get() = alertas.firstOrNull { it.activa && it.tipo == "retraso" }
    val proximoPunto: PuntoControl? get() = puntos.firstOrNull { it.horaReal == null }
    val puntosDeControl: Int get() = puntos.count { it.tipo == "control" }

    companion object {
        fun desde(j: JSONObject): Viaje {
            val cliente = j.optJSONObject("cliente") ?: JSONObject()
            val origen = j.optJSONObject("origen") ?: JSONObject()
            val destino = j.optJSONObject("destino") ?: JSONObject()
            val carga = j.optJSONObject("carga") ?: JSONObject()
            val plan = j.optJSONObject("plan") ?: JSONObject()
            val progreso = j.optJSONObject("progreso") ?: JSONObject()
            val id = j.entero("id") ?: 0
            return Viaje(
                id = id,
                codigo = j.texto("codigo") ?: "ENV-$id",
                estado = j.texto("estado") ?: "asignado",
                cliente = cliente.texto("nombre") ?: "",
                origenDireccion = origen.texto("direccion") ?: "",
                origenContacto = origen.texto("contacto"),
                origenCelular = origen.texto("celular"),
                destinoDireccion = destino.texto("direccion") ?: "",
                destinatario = destino.texto("destinatario"),
                destinoCelular = destino.texto("celular"),
                cargaTipo = carga.texto("tipo_nombre") ?: "",
                descripcion = carga.texto("descripcion") ?: "",
                empaque = carga.texto("empaque_nombre") ?: "",
                cantidad = carga.entero("cantidad"),
                pesoKg = carga.entero("peso_kg") ?: 0,
                cargueProgramado = j.texto("cargue_programado"),
                distanciaM = plan.decimal("distancia_m") ?: 0.0,
                duracionS = plan.entero("duracion_s") ?: 0,
                idRecorrido = progreso.texto("id_recorrido"),
                salidaReal = progreso.texto("salida_real"),
                avanceM = progreso.entero("avance_m") ?: 0,
                porcentaje = progreso.entero("porcentaje") ?: 0,
                recorridoM = progreso.entero("recorrido_m") ?: 0,
                retrasoS = progreso.entero("retraso_s"),
                nivelRetraso = progreso.texto("nivel_retraso"),
                desvioM = progreso.entero("desvio_m"),
                velocidadKmh = progreso.entero("velocidad_kmh"),
                llegadaEstimada = progreso.texto("llegada_estimada"),
                puntos = j.optJSONArray("puntos_control").objetos().map { p ->
                    PuntoControl(
                        orden = p.entero("orden") ?: 0,
                        tipo = p.texto("tipo") ?: "control",
                        nombre = p.texto("nombre") ?: "",
                        distanciaM = p.decimal("distancia_m") ?: 0.0,
                        horaPlan = p.texto("hora_plan"),
                        horaReal = p.texto("hora_real"),
                        retrasoS = p.entero("retraso_s"),
                        nivel = p.texto("nivel") ?: "pendiente"
                    )
                },
                alertas = j.optJSONArray("alertas").objetos().map { a ->
                    AlertaViaje(
                        id = a.entero("id") ?: 0,
                        tipo = a.texto("tipo") ?: "",
                        activa = a.optBoolean("activa", false),
                        valorMax = a.decimal("valor_max")
                    )
                },
                conductor = j.optJSONObject("conductor")?.texto("nombre"),
                json = j
            )
        }
    }
}

// ====================================================================
//  UBICACIÓN ACTUAL (para "Disponible" y para avisar dónde está el camión)
// ====================================================================
object UbicacionActual {

    fun tienePermiso(contexto: Context): Boolean =
        ContextCompat.checkSelfPermission(contexto, Manifest.permission.ACCESS_FINE_LOCATION) ==
                PackageManager.PERMISSION_GRANTED

    /** Una lectura del GPS (máximo 15 s). null si no hay permiso, señal o se agotó el tiempo. */
    @SuppressLint("MissingPermission")
    suspend fun obtener(contexto: Context): Location? {
        if (!tienePermiso(contexto)) return null
        val cliente = LocationServices.getFusedLocationProviderClient(contexto)
        return try {
            withTimeoutOrNull(15_000L) {
                suspendCancellableCoroutine<Location?> { cont ->
                    val cancelador = CancellationTokenSource()
                    cont.invokeOnCancellation { cancelador.cancel() }
                    cliente.getCurrentLocation(Priority.PRIORITY_HIGH_ACCURACY, cancelador.token)
                        .addOnSuccessListener { if (cont.isActive) cont.resume(it) }
                        .addOnFailureListener { if (cont.isActive) cont.resume(null) }
                }
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            null
        }
    }
}
