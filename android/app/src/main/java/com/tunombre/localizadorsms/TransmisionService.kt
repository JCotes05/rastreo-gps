package com.tunombre.localizadorsms

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.location.Location
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import com.google.android.gms.tasks.CancellationTokenSource
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

// ====================================================================
//  CONFIGURACIÓN FIJA — la app ya no pide la IP: siempre envía aquí.
//  Si algún día cambia el servidor, se cambia en este único lugar.
// ====================================================================
object Config {
    const val IP_SERVIDOR = "44.217.7.177"
    const val PUERTO = 5000
    const val INTERVALO_MS = 5_000L   // cada cuánto se envía una ubicación

    // Una lectura de GPS trae un "radio de error" (en metros). Si es mayor que
    // esto (típico entre edificios, túneles o recién encendido el GPS), el
    // punto puede estar a decenas de metros de la calle real y dibujaría
    // la ruta fuera de ella: es mejor NO enviarlo y esperar a la siguiente.
    const val PRECISION_MAXIMA_M = 30f

    // P3-S2: API de la página (registro del camión, disponibilidad y viajes).
    // Todas las instancias comparten la misma base de datos, así que sirve cualquiera.
    const val URL_API = "https://jesuscotes.sytes.net"
    const val CONSULTA_VIAJE_MS = 15_000L   // cada cuánto se pregunta si hay viaje / cómo va
    const val LATIDO_MS = 60_000L           // cada cuánto se avisa la ubicación mientras está disponible
}

// ====================================================================
//  ESTADO COMPARTIDO ENTRE EL SERVICIO Y LA PANTALLA
//  El servicio (que corre en segundo plano) escribe aquí; la pantalla
//  solo lee. Como son "states" de Compose, la pantalla se redibuja sola
//  cada vez que algo cambia. Vive mientras viva el proceso de la app.
// ====================================================================
object EstadoTransmision {
    var activa by mutableStateOf(false)              // ¿el servicio está enviando?
    var mensaje by mutableStateOf("Listo para enviar")
    var hayError by mutableStateOf(false)
    var enviados by mutableStateOf(0)                // ubicaciones enviadas en este recorrido
    var ultimoEnvio by mutableStateOf("")            // hora (HH:mm:ss) del último envío exitoso
    var conductorNombre by mutableStateOf<String?>(null)   // P3-S1: conductor verificado del recorrido en curso
    var conductorCedula by mutableStateOf<String?>(null)   //        (null = se transmite sin verificar)
    var envioId by mutableStateOf<Int?>(null)              // P3-S2: envío que se está transmitiendo (null = sin viaje)
    var posicion by mutableStateOf<Pair<Double, Double>?>(null)   // última posición enviada (para el mapa)
}

/**
 * Servicio en PRIMER PLANO (tipo "location"): es lo que permite que la app
 * siga enviando la ubicación aunque se bloquee el celular o se cambie de app.
 *
 *  - Android obliga a que un servicio así muestre una notificación permanente;
 *    esa es la notificación de "Enviando ubicación" (con botón "Detener").
 *  - Un WakeLock parcial evita que el procesador se duerma con la pantalla
 *    apagada (si se durmiera, los temporizadores se detendrían).
 *  - START_STICKY: si el sistema cierra la app por falta de memoria, intenta
 *    volver a iniciar el servicio y continúa el MISMO recorrido.
 */
class TransmisionService : Service() {

    companion object {
        const val ACCION_INICIAR = "com.tunombre.localizadorsms.INICIAR"
        const val ACCION_DETENER = "com.tunombre.localizadorsms.DETENER"
        const val ACCION_SENSORES = "com.tunombre.localizadorsms.SENSORES"

        private const val CANAL_ID = "transmision_ubicacion"
        private const val NOTIFICACION_ID = 1001
        private const val PREFS = "rastreo_prefs"
        private const val PREF_ACTIVA = "activa"
        private const val PREF_RECORRIDO = "id_recorrido"

        // P3-S1: conductor verificado por reconocimiento facial
        private const val EXTRA_CEDULA = "cedula_conductor"
        private const val EXTRA_NOMBRE = "nombre_conductor"
        private const val PREF_CEDULA = "cedula_conductor"
        private const val PREF_NOMBRE = "nombre_conductor"
        private val PATRON_CEDULA = Regex("^[0-9]{5,15}$")     // igual que en el servidor

        // P3-S2: envío (viaje) que se transmite
        private const val EXTRA_ENVIO = "id_envio"
        private const val EXTRA_RECORRIDO = "id_recorrido_previo"
        private const val PREF_ENVIO = "id_envio"

        // La pantalla llama a estas dos funciones; no necesita saber más.
        // cedula = null -> se transmite sin conductor verificado (planilla en blanco).
        // idEnvio = null -> se transmite sin viaje (solo rastreo, como en S1).
        // recorridoPrevio = el recorrido que ya tiene el viaje (al reanudar), para no partirlo en dos.
        fun iniciar(
            contexto: Context,
            cedula: String? = null,
            nombre: String? = null,
            idEnvio: Int? = null,
            recorridoPrevio: Long? = null
        ) {
            val intent = Intent(contexto, TransmisionService::class.java)
                .setAction(ACCION_INICIAR)
                .putExtra(EXTRA_CEDULA, cedula)
                .putExtra(EXTRA_NOMBRE, nombre)
                .putExtra(EXTRA_ENVIO, idEnvio ?: 0)
                .putExtra(EXTRA_RECORRIDO, recorridoPrevio ?: 0L)
            ContextCompat.startForegroundService(contexto, intent)
        }

        fun detener(contexto: Context) {
            contexto.stopService(Intent(contexto, TransmisionService::class.java))
        }

        // P3-S3: se eligió (o cambió) el ESP de sensores en pleno viaje: el servicio se reconecta.
        // Solo se llama con la app a la vista y el servicio ya corriendo.
        fun reconectarSensores(contexto: Context) {
            try {
                contexto.startService(Intent(contexto, TransmisionService::class.java).setAction(ACCION_SENSORES))
            } catch (e: Exception) {
                // el servicio no está corriendo o el sistema no lo permite: se conectará al reanudar
            }
        }
    }

    private lateinit var fusedLocationClient: FusedLocationProviderClient
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var bucle: Job? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var cedulaConductor: String? = null      // P3-S1: viaja en cada paquete
    private var nombreConductor: String? = null      //        se muestra en la notificación
    private var idEnvio: Int? = null                 // P3-S2: viaja en cada paquete como ENVIO:<id>
    private var trabajoSensores: Job? = null         // P3-S3: conexión con el ESP32 de sensores de la carga

    override fun onCreate() {
        super.onCreate()
        fusedLocationClient = LocationServices.getFusedLocationProviderClient(this)
        crearCanalDeNotificacion()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Botón "Detener" de la notificación.
        if (intent?.action == ACCION_DETENER) {
            stopSelf()                       // onDestroy se encarga de la limpieza
            return START_NOT_STICKY
        }

        // P3-S3: cambiaron el ESP de sensores en pleno viaje.
        if (intent?.action == ACCION_SENSORES) {
            if (bucle?.isActive == true) {
                trabajoSensores?.cancel()
                trabajoSensores = null
                iniciarSensores()
                return START_STICKY
            }
            stopSelf()
            return START_NOT_STICKY
        }

        // Si ya estaba enviando (por ejemplo, un doble toque), no se duplica nada.
        if (bucle?.isActive == true) return START_STICKY

        val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
        val esReinicioDelSistema = (intent == null)     // el sistema relanzó el servicio tras cerrarlo
        if (esReinicioDelSistema && !prefs.getBoolean(PREF_ACTIVA, false)) {
            stopSelf()
            return START_NOT_STICKY
        }

        // Un recorrido NUEVO tiene un ID nuevo; si es un reinicio del sistema se
        // conserva el mismo para que el recorrido no se parta en dos.
        // P3-S2: al reanudar un viaje que ya salió, se usa el recorrido que ya tiene.
        val idRecorrido = if (esReinicioDelSistema) {
            prefs.getLong(PREF_RECORRIDO, System.currentTimeMillis())
        } else {
            intent?.getLongExtra(EXTRA_RECORRIDO, 0L)?.takeIf { it > 0L } ?: System.currentTimeMillis()
        }
        idEnvio = if (esReinicioDelSistema) {
            prefs.getInt(PREF_ENVIO, 0).takeIf { it > 0 }
        } else {
            intent?.getIntExtra(EXTRA_ENVIO, 0)?.takeIf { it > 0 }
        }

        // P3-S1: conductor verificado (o ninguno). En un reinicio del sistema se
        // recupera el mismo de antes, igual que el recorrido.
        if (esReinicioDelSistema) {
            cedulaConductor = prefs.getString(PREF_CEDULA, null)
            nombreConductor = prefs.getString(PREF_NOMBRE, null)
        } else {
            cedulaConductor = intent?.getStringExtra(EXTRA_CEDULA)?.trim()?.takeIf { PATRON_CEDULA.matches(it) }
            nombreConductor = if (cedulaConductor != null) intent?.getStringExtra(EXTRA_NOMBRE) else null
        }

        if (!pasarAPrimerPlano()) {
            EstadoTransmision.activa = false
            stopSelf()
            return START_NOT_STICKY
        }

        prefs.edit()
            .putBoolean(PREF_ACTIVA, true)
            .putLong(PREF_RECORRIDO, idRecorrido)
            .putString(PREF_CEDULA, cedulaConductor)        // null = se borra
            .putString(PREF_NOMBRE, nombreConductor)
            .putInt(PREF_ENVIO, idEnvio ?: 0)
            .apply()
        if (!esReinicioDelSistema) {
            EstadoTransmision.enviados = 0
            EstadoTransmision.posicion = null        // el mapa no muestra la posición de un recorrido anterior
        }
        EstadoTransmision.conductorCedula = cedulaConductor
        EstadoTransmision.conductorNombre = nombreConductor
        EstadoTransmision.envioId = idEnvio
        EstadoTransmision.activa = true
        EstadoTransmision.hayError = false
        EstadoTransmision.mensaje = "Buscando tu ubicación GPS…"

        tomarWakeLock()
        iniciarBucle(idRecorrido)
        iniciarSensores()
        return START_STICKY
    }

    // P3-S3: si el viaje tiene envío y hay un ESP de sensores elegido, se conecta durante todo el viaje.
    // Si no hay ESP o no responde, el viaje sigue igual (GPS, monitoreo y alertas de S2).
    private fun iniciarSensores() {
        val envio = idEnvio ?: return
        val equipo = Sensores.guardado(this) ?: return
        if (trabajoSensores?.isActive == true) return
        trabajoSensores = scope.launch {
            ConexionSensores(applicationContext, equipo.direccion, envio).correr()
        }
    }

    override fun onDestroy() {
        bucle?.cancel()
        trabajoSensores?.cancel()            // le manda FIN al ESP y cierra el Bluetooth
        scope.cancel()
        soltarWakeLock()
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putBoolean(PREF_ACTIVA, false).apply()
        EstadoTransmision.activa = false
        EstadoTransmision.hayError = false
        EstadoTransmision.mensaje = "Envío detenido"
        EstadoTransmision.conductorNombre = null     // el próximo recorrido se verifica de nuevo
        EstadoTransmision.conductorCedula = null
        EstadoTransmision.envioId = null
        super.onDestroy()
    }

    // ---------------------------------------------------------------
    //  Bucle principal: una ubicación cada INTERVALO_MS, sin solaparse
    // ---------------------------------------------------------------
    private fun iniciarBucle(idRecorrido: Long) {
        bucle = scope.launch {
            while (isActive) {
                val inicio = SystemClock.elapsedRealtime()
                enviarUbicacionUnaVez(idRecorrido)
                // Se descuenta lo que tardó el GPS/la red para mantener el ritmo.
                val gastado = SystemClock.elapsedRealtime() - inicio
                delay(maxOf(500L, Config.INTERVALO_MS - gastado))
            }
        }
    }

    private suspend fun enviarUbicacionUnaVez(idRecorrido: Long) {
        if (!tienePermisoUbicacionPrecisa()) {
            publicarError("Falta el permiso de ubicación precisa")
            return
        }

        val ubicacion: Location? = try {
            // Si el GPS no responde en 15 s, se da por fallida esta lectura y se sigue.
            withTimeoutOrNull(15_000L) { obtenerUbicacion() }
        } catch (e: CancellationException) {
            throw e                          // el servicio se está deteniendo: no es un error
        } catch (e: SecurityException) {
            publicarError("Falta el permiso de ubicación")
            return
        } catch (e: Exception) {
            publicarError("Error al obtener la ubicación: ${e.message}")
            return
        }

        if (ubicacion == null) {
            publicarError("Sin señal de GPS. ¿Tienes la ubicación activada?")
            return
        }

        // Lectura imprecisa: se descarta (no es un error, solo se espera la siguiente).
        // hasAccuracy() es false si el teléfono no informa el radio de error; en ese caso se envía igual.
        if (ubicacion.hasAccuracy() && ubicacion.accuracy > Config.PRECISION_MAXIMA_M) {
            publicarEspera("Esperando mejor señal GPS (±${ubicacion.accuracy.toInt()} m)")
            return
        }

        // Locale.US: garantiza punto decimal ("4.60972") y no coma ("4,60972"), que rompería el formato del mensaje.
        val lat = String.format(Locale.US, "%.5f", ubicacion.latitude)
        val lon = String.format(Locale.US, "%.5f", ubicacion.longitude)
        // ubicacion.time = hora que marcó el GPS al tomar la lectura.
        val horaGps = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date(ubicacion.time))
        // P3-S1: si hay conductor verificado, su cédula viaja en CADA paquete (así lo hacen
        // los equipos de rastreo reales: si se pierde un paquete UDP, el siguiente la trae).
        // P3-S2: el envío (viaje) y la velocidad que mide el GPS (m/s -> km/h). Los campos
        // se leen por su NOMBRE, así que un servidor sin el paso 4 simplemente los ignora.
        val velocidad = if (ubicacion.hasSpeed()) String.format(Locale.US, "%.1f", ubicacion.speed * 3.6f) else null
        val mensaje = "LAT:$lat,LON:$lon,HORA:$horaGps,RECORRIDO:$idRecorrido" +
                (cedulaConductor?.let { ",CONDUCTOR:$it" } ?: "") +
                (idEnvio?.let { ",ENVIO:$it" } ?: "") +
                (velocidad?.let { ",VEL:$it" } ?: "")

        val (ok, detalle) = enviarUDP(Config.IP_SERVIDOR, Config.PUERTO, mensaje)
        // P3-S3: los eventos de la carga se ubican con la última posición del GPS.
        EstadoSensores.ultimaPosicion = ubicacion.latitude to ubicacion.longitude
        if (ok) {
            EstadoTransmision.posicion = ubicacion.latitude to ubicacion.longitude
            publicarExito()
        } else {
            publicarError("No se pudo enviar: $detalle")
        }
    }

    // Convierte la API de "callbacks" del GPS en una función que se puede esperar.
    private suspend fun obtenerUbicacion(): Location? = suspendCancellableCoroutine { cont ->
        val cancelador = CancellationTokenSource()
        cont.invokeOnCancellation { cancelador.cancel() }
        try {
            fusedLocationClient.getCurrentLocation(Priority.PRIORITY_HIGH_ACCURACY, cancelador.token)
                .addOnSuccessListener { ubicacion -> if (cont.isActive) cont.resume(ubicacion) }
                .addOnFailureListener { error -> if (cont.isActive) cont.resumeWithException(error) }
        } catch (e: SecurityException) {
            if (cont.isActive) cont.resumeWithException(e)
        }
    }

    // Un único datagrama UDP ("dispara y olvida"). La red no se puede usar en el hilo principal.
    private suspend fun enviarUDP(ip: String, puerto: Int, mensaje: String): Pair<Boolean, String> =
        withContext(Dispatchers.IO) {
            var socket: DatagramSocket? = null
            try {
                socket = DatagramSocket()
                val datos = mensaje.toByteArray(Charsets.UTF_8)
                socket.send(DatagramPacket(datos, datos.size, InetAddress.getByName(ip), puerto))
                true to "Datagrama enviado"
            } catch (e: Exception) {
                false to (e.message ?: "Error UDP desconocido")
            } finally {
                socket?.close()
            }
        }

    private fun tienePermisoUbicacionPrecisa(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) ==
                PackageManager.PERMISSION_GRANTED

    // ---------------------------------------------------------------
    //  Estado + notificación
    // ---------------------------------------------------------------
    private fun publicarExito() {
        val hora = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())
        EstadoTransmision.enviados += 1
        EstadoTransmision.ultimoEnvio = hora
        EstadoTransmision.hayError = false
        EstadoTransmision.mensaje = "Enviando ubicación"
        actualizarNotificacion("Último envío: $hora · ${EstadoTransmision.enviados} enviados")
    }

    // Aviso no crítico (p. ej. señal GPS floja): se muestra, pero no se pinta como error.
    private fun publicarEspera(texto: String) {
        EstadoTransmision.hayError = false
        EstadoTransmision.mensaje = texto
        actualizarNotificacion(texto)
    }

    private fun publicarError(texto: String) {
        EstadoTransmision.hayError = true
        EstadoTransmision.mensaje = texto
        actualizarNotificacion(texto)
    }

    private fun crearCanalDeNotificacion() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val canal = NotificationChannel(
                CANAL_ID,
                "Orbita: envío de ubicación",
                NotificationManager.IMPORTANCE_LOW          // visible pero sin sonido ni vibración
            ).apply {
                description = "Aviso permanente mientras la app está enviando tu ubicación"
                setShowBadge(false)
            }
            (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
                .createNotificationChannel(canal)
        }
    }

    private fun construirNotificacion(texto: String): Notification {
        val banderas = PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT

        // Tocar la notificación abre la app.
        val abrirApp = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            banderas
        )
        // Botón "Detener": útil con el celular bloqueado.
        val detener = PendingIntent.getService(
            this, 1,
            Intent(this, TransmisionService::class.java).setAction(ACCION_DETENER),
            banderas
        )

        return NotificationCompat.Builder(this, CANAL_ID)
            .setSmallIcon(R.drawable.ic_stat_orbita)
            .setColor(0xFF0096FE.toInt())                       // azul de la marca Orbita
            .setContentTitle(tituloNotificacion())
            .setContentText(texto)
            .setContentIntent(abrirApp)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Detener", detener)
            .setOngoing(true)                                   // no se puede descartar deslizando
            .setOnlyAlertOnce(true)                             // actualizar el texto no vuelve a "sonar"
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC) // también se ve con el celular bloqueado
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    // "Orbita · ENV-0007 · Jesús Cotes", "Orbita · Jesús Cotes" o "Orbita · enviando ubicación"
    private fun tituloNotificacion(): String {
        val partes = listOfNotNull(idEnvio?.let { String.format(Locale.US, "ENV-%04d", it) }, nombreConductor)
        return if (partes.isEmpty()) "Orbita · enviando ubicación" else "Orbita · " + partes.joinToString(" · ")
    }

    // Se vuelve a publicar con el mismo ID: reemplaza el texto (y, si alguien la descartó, la restituye).
    private fun actualizarNotificacion(texto: String) {
        (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
            .notify(NOTIFICACION_ID, construirNotificacion(texto))
    }

    // Convierte al servicio en "primer plano": desde aquí Android muestra la
    // notificación y no lo cierra mientras la app está en segundo plano.
    private fun pasarAPrimerPlano(): Boolean {
        val notificacion = construirNotificacion("Iniciando…")
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(NOTIFICACION_ID, notificacion, ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION)
            } else {
                startForeground(NOTIFICACION_ID, notificacion)
            }
            true
        } catch (e: Exception) {
            EstadoTransmision.hayError = true
            EstadoTransmision.mensaje = "No se pudo iniciar el envío en segundo plano: ${e.message}"
            false
        }
    }

    // ---------------------------------------------------------------
    //  WakeLock: mantiene despierto el procesador con la pantalla apagada
    // ---------------------------------------------------------------
    private fun tomarWakeLock() {
        if (wakeLock?.isHeld == true) return
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "rastreo:transmision").apply {
            setReferenceCounted(false)
            acquire()                        // se libera en onDestroy
        }
    }

    private fun soltarWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }
}