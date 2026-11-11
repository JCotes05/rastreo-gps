package com.tunombre.localizadorsms

import android.Manifest
import android.bluetooth.BluetoothAdapter
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONObject
import java.util.Locale

// ── Identidad Orbita (los mismos colores y la misma tipografía de la página web) ─────────────
internal val Oscuro = Color(0xFF0B1F3A)        // --oscuro de la página
internal val FondoAbajo = Color(0xFF06142A)    // el mismo azul, un poco más profundo, para el degradado
internal val Medio = Color(0xFF1F3B5C)         // --medio
internal val GrisAzul = Color(0xFFA7B1BE)      // --gris-azul
internal val Claro = Color(0xFFE6E9EF)         // --claro
internal val AzulMarca = Color(0xFF0096FE)     // azul del logo
internal val CianMarca = Color(0xFF2DDBFC)     // brillo cian del logo
internal val NaranjaMarca = Color(0xFFE4572E)  // acento naranja de la marca
internal val Ambar = Color(0xFFF2A900)

// Manrope: la tipografía de la página. Los archivos van en res/font/.
internal val Manrope = FontFamily(
    Font(R.font.manrope_semibold, FontWeight.SemiBold),
    Font(R.font.manrope_extrabold, FontWeight.ExtraBold)
)

/** P3-S1: en qué va la verificación del conductor antes de transmitir. */
sealed class FaseVerificacion {
    object Inactiva : FaseVerificacion()
    data class EnCurso(val texto: String, val vista: VistaPrevia? = null) : FaseVerificacion()   // vista: P3-S3
    data class Fallida(val titulo: String, val detalle: String) : FaseVerificacion()
}

/** Cómo se ve el botón principal. */
enum class EstadoBoton { REPOSO, VERIFICANDO, ACTIVO }

/**
 * Pantalla de la app.
 *
 * P3-S2: primero el camión ingresa (placa + contraseña) o se registra. Con la sesión abierta:
 *   - sin viaje: se marca "Disponible" y la app avisa su ubicación cada minuto;
 *   - con viaje asignado: se ve la ruta y, al tocar "Iniciar viaje", se verifica al
 *     conductor (S1) y empieza la transmisión con el número del envío en cada paquete;
 *   - en viaje: avance, retraso, puntos de control y alertas, y "Finalizar viaje".
 * La app pregunta al servidor cada 15 s si hay viaje y cómo va.
 *
 * P3-S1 sigue igual: el botón radar verifica al conductor por Bluetooth y luego transmite.
 * Esta pantalla NO hace el envío: se lo ordena al servicio (TransmisionService),
 * que sigue trabajando aunque se cierre la pantalla o se bloquee el celular.
 */
class MainActivity : ComponentActivity() {

    // ¿Están permitidas las notificaciones? (en Android 13+ hay que aceptarlas)
    private var notificacionesActivas by mutableStateOf(true)

    // P3-S1: verificación del conductor
    private var fase by mutableStateOf<FaseVerificacion>(FaseVerificacion.Inactiva)
    private var pcElegido by mutableStateOf<EquipoBluetooth?>(null)
    private var mostrarSelector by mutableStateOf(false)
    private var equiposVinculados by mutableStateOf<List<EquipoBluetooth>>(emptyList())
    private var verificarAlElegir = false          // el selector se abrió al tocar el botón principal
    private var trabajoVerificacion: Job? = null

    // P3-S3: ESP32 de sensores de la carga
    private var sensoresElegido by mutableStateOf<EquipoBluetooth?>(null)
    private var mostrarSelectorSensores by mutableStateOf(false)

    // P3-S2: sesión del camión y viaje
    private var token by mutableStateOf<String?>(null)
    private var camion by mutableStateOf<Camion?>(null)
    private var viaje by mutableStateOf<Viaje?>(null)
    private var rutaJson by mutableStateOf<String?>(null)        // línea planeada del viaje (para el mapa)
    private var rutaDeEnvio: Int? = null                         // de qué envío es esa línea
    private var posicionTelefono by mutableStateOf<Pair<Double, Double>?>(null)
    private var aviso by mutableStateOf<String?>(null)
    private var avisoEsError by mutableStateOf(false)
    private var ultimoErrorConsulta: String? = null
    private var sinConexion by mutableStateOf(false)
    private var cambiandoDisponibilidad by mutableStateOf(false)
    private var finalizando by mutableStateOf(false)
    private var accesoCargando by mutableStateOf(false)
    private var accesoError by mutableStateOf<String?>(null)
    private var accesoCampo by mutableStateOf<String?>(null)
    private var modoLibre by mutableStateOf(false)               // pantalla de S1: transmitir sin viaje
    private var envioPorIniciar: Viaje? = null                   // viaje que arranca al terminar la verificación
    private var despuesDePermisos: (() -> Unit)? = null
    private var yaConsulto = false
    private val candado = Mutex()                                // una consulta al servidor a la vez

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) {
        // Se mira el estado REAL de cada permiso (el mapa del resultado solo trae los que se pidieron ahora).
        val luego = despuesDePermisos
        despuesDePermisos = null
        if (tienePermisoUbicacionPrecisa()) {
            luego?.invoke()
        } else {
            val texto = "Necesitamos el permiso de ubicación PRECISA para poder enviar tu posición"
            EstadoTransmision.hayError = true
            EstadoTransmision.mensaje = texto
            mostrarAviso(texto, error = true)
        }
    }

    // Solo el permiso de Bluetooth (para "Cambiar equipo" cuando aún no se ha dado).
    private var selectorTrasPermiso: () -> Unit = {}
    private val permisoBluetoothLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { concedido ->
        if (concedido) selectorTrasPermiso()
    }

    // Ventana del sistema "¿Activar Bluetooth?".
    private val activarBluetoothLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) {
        if (VerificacionConductor.adaptador(this)?.isEnabled == true) {
            prepararVerificacion()
        } else {
            fase = FaseVerificacion.Fallida(
                "Bluetooth apagado",
                "Sin Bluetooth el celular no puede hablar con el equipo de reconocimiento."
            )
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        configurarBarrasDelSistema()
        pcElegido = VerificacionConductor.pcGuardado(this)
        sensoresElegido = Sensores.guardado(this)
        token = Sesion.token(this)
        camion = if (token != null) Sesion.camionGuardado(this) else null

        setContent {
            OrbitaTema {
                Raiz()
            }
        }

        // Mientras la app esté a la vista: preguntar por el viaje y avisar la ubicación.
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                launch {
                    while (true) {
                        token?.let { consultarViaje(it) }
                        delay(Config.CONSULTA_VIAJE_MS)
                    }
                }
                launch {
                    while (true) {
                        latido()
                        delay(Config.LATIDO_MS)
                    }
                }
                // P3-S3: eventos de la carga que quedaron en cola (sin señal al final del viaje).
                // Mientras se transmite, los sube el servicio; aquí solo lo que sobró.
                launch {
                    while (true) {
                        delay(30_000L)
                        if (token != null && !EstadoTransmision.activa && ColaEventos.cantidad(this@MainActivity) > 0) {
                            ColaEventos.vaciar(this@MainActivity)
                        }
                    }
                }
            }
        }
    }

    @Composable
    private fun Raiz() {
        val c = camion
        val v = viaje
        val libreActiva = EstadoTransmision.activa && EstadoTransmision.envioId == null
        when {
            token == null || c == null -> PantallaAcceso(
                cargando = accesoCargando,
                error = accesoError,
                campoError = accesoCampo,
                onIngresar = { placa, clave -> ingresar(placa, clave) },
                onRegistrar = { datos -> registrar(datos) },
                onLimpiarError = {
                    accesoError = null
                    accesoCampo = null
                }
            )
            libreActiva || (modoLibre && v == null) -> PantallaPrincipal(
                activa = EstadoTransmision.activa,
                mensaje = EstadoTransmision.mensaje,
                hayError = EstadoTransmision.hayError,
                enviados = EstadoTransmision.enviados,
                ultimoEnvio = EstadoTransmision.ultimoEnvio,
                notificacionesActivas = notificacionesActivas,
                fase = fase,
                conductorNombre = EstadoTransmision.conductorNombre,
                conductorCedula = EstadoTransmision.conductorCedula,
                pcNombre = pcElegido?.nombre,
                onEnviarClick = { alternarEnvio() },
                onReintentar = {
                    envioPorIniciar = null
                    pedirPermisos(conBluetooth = true) { prepararVerificacion() }
                },
                onSinVerificar = { empezarEnvio(null, null) },
                onCambiarPc = { cambiarComputador() },
                avisoViaje = v?.let { "Tienes un viaje asignado (${it.codigo}). Detén esta transmisión para iniciarlo." },
                onVolver = if (!EstadoTransmision.activa && fase !is FaseVerificacion.EnCurso) {
                    {
                        modoLibre = false
                        fase = FaseVerificacion.Inactiva
                    }
                } else null
            )
            else -> PantallaCamion(
                camion = c,
                viaje = v,
                rutaJson = rutaJson,
                posicionTelefono = posicionTelefono,
                fase = fase,
                aviso = aviso,
                avisoEsError = avisoEsError,
                sinConexion = sinConexion,
                cambiandoDisponibilidad = cambiandoDisponibilidad,
                finalizando = finalizando,
                pcNombre = pcElegido?.nombre,
                sensoresNombre = sensoresElegido?.nombre,
                acciones = acciones
            )
        }
        // Atrás, en la pantalla de S1, vuelve al panel del camión (si no está transmitiendo ni verificando).
        BackHandler(enabled = modoLibre && !EstadoTransmision.activa && fase !is FaseVerificacion.EnCurso) {
            modoLibre = false
            fase = FaseVerificacion.Inactiva
        }
        if (mostrarSelector) {
            SelectorDeComputador(
                equipos = equiposVinculados,
                onElegir = { elegirPc(it) },
                onCerrar = {
                    mostrarSelector = false
                    verificarAlElegir = false
                }
            )
        }
        if (mostrarSelectorSensores) {
            SelectorDeComputador(
                equipos = equiposVinculados,
                titulo = "Sensores de la carga",
                ayuda = "Elige el ESP32 de sensores (por ejemplo \"ORBITA-SENSORES\"). Se conecta solo al iniciar " +
                        "un viaje. Sin sensores, el viaje funciona igual.",
                sinEquipos = "No hay equipos vinculados. En los ajustes de Bluetooth del celular, vincula el ESP32 " +
                        "de sensores (\"ORBITA-SENSORES\") y vuelve a intentarlo.",
                textoQuitar = if (sensoresElegido != null) "No usar sensores" else null,
                onQuitar = { elegirSensores(null) },
                onElegir = { elegirSensores(it) },
                onCerrar = { mostrarSelectorSensores = false }
            )
        }
    }

    private val acciones = AccionesCamion(
        onDisponible = { cambiarDisponibilidad(it) },
        onBotonViaje = { botonViaje() },
        onSinVerificar = { empezarEnvio(null, null) },
        onFinalizar = { finalizarViaje() },
        onDetener = { TransmisionService.detener(this) },
        onTransmitirSinViaje = {
            fase = FaseVerificacion.Inactiva
            modoLibre = true
        },
        onActualizar = { token?.let { t -> lifecycleScope.launch { consultarViaje(t) } } },
        onCambiarPc = { cambiarComputador() },
        onCambiarSensores = { cambiarSensores() },
        onCerrarSesion = { cerrarSesion() },
        onCerrarAviso = { aviso = null },
        onLlamar = { numero ->
            try {
                startActivity(Intent(Intent.ACTION_DIAL, Uri.parse("tel:$numero")))
            } catch (e: Exception) {
                mostrarAviso("Este celular no puede hacer llamadas.", error = true)
            }
        }
    )

    // La pantalla es oscura de borde a borde: barras transparentes con iconos claros.
    @Suppress("DEPRECATION")
    private fun configurarBarrasDelSistema() {
        WindowCompat.setDecorFitsSystemWindows(window, false)
        window.statusBarColor = android.graphics.Color.TRANSPARENT
        window.navigationBarColor = android.graphics.Color.TRANSPARENT
        if (Build.VERSION.SDK_INT >= 29) {
            window.isNavigationBarContrastEnforced = false
        }
        val controlador = WindowInsetsControllerCompat(window, window.decorView)
        controlador.isAppearanceLightStatusBars = false
        controlador.isAppearanceLightNavigationBars = false
    }

    override fun onResume() {
        super.onResume()
        notificacionesActivas = NotificationManagerCompat.from(this).areNotificationsEnabled()
    }

    override fun onDestroy() {
        // Si se cierra la pantalla en plena verificación, se corta la conexión Bluetooth.
        if (fase is FaseVerificacion.EnCurso) VerificacionConductor.cancelar()
        super.onDestroy()
    }

    private fun mostrarAviso(texto: String, error: Boolean) {
        aviso = texto
        avisoEsError = error
    }

    // ---------------------------------------------------------------
    //  P3-S2 · Sesión del camión
    // ---------------------------------------------------------------
    private fun ingresar(placa: String, clave: String) {
        if (accesoCargando) return
        accesoCargando = true
        accesoError = null
        accesoCampo = null
        lifecycleScope.launch {
            val r = ApiOrbita.ingresar(placa, clave)
            accesoCargando = false
            when (r) {
                is Respuesta.Ok -> sesionAbierta(r.json, bienvenida = null)
                is Respuesta.Error -> {
                    accesoError = r.mensaje
                    accesoCampo = r.campo
                }
            }
        }
    }

    private fun registrar(datos: JSONObject) {
        if (accesoCargando) return
        accesoCargando = true
        accesoError = null
        accesoCampo = null
        lifecycleScope.launch {
            val r = ApiOrbita.registrar(datos)
            accesoCargando = false
            when (r) {
                is Respuesta.Ok -> sesionAbierta(
                    r.json,
                    bienvenida = "Camión ${datos.optString("placa")} registrado. Activa \"Disponible\" cuando puedas recibir viajes."
                )
                is Respuesta.Error -> {
                    accesoError = r.mensaje
                    accesoCampo = r.campo
                }
            }
        }
    }

    private fun sesionAbierta(json: JSONObject, bienvenida: String?) {
        val nuevoToken = json.texto("token")
        val datosCamion = json.optJSONObject("camion")
        if (nuevoToken == null || datosCamion == null) {
            accesoError = "El servidor respondió algo inesperado. Intenta de nuevo."
            return
        }
        Sesion.guardar(this, nuevoToken, datosCamion)
        token = nuevoToken
        camion = Camion.desde(datosCamion)
        viaje = null
        rutaJson = null
        rutaDeEnvio = null
        yaConsulto = false
        ultimoErrorConsulta = null
        aviso = null
        if (bienvenida != null) mostrarAviso(bienvenida, error = false)
        lifecycleScope.launch { consultarViaje(nuevoToken) }
    }

    private fun cerrarSesion() {
        if (EstadoTransmision.activa) {
            mostrarAviso("Detén la transmisión antes de cerrar la sesión.", error = true)
            return
        }
        val t = token
        val estabaDisponible = camion?.estado == "disponible"
        olvidarSesion()
        if (t != null) {
            lifecycleScope.launch {
                // Un camión sin sesión no debe quedar ofreciéndose para viajes.
                if (estabaDisponible) ApiOrbita.disponibilidad(t, false, null, null)
                ApiOrbita.salir(t)
            }
        }
    }

    private fun olvidarSesion() {
        Sesion.borrar(this)
        token = null
        camion = null
        viaje = null
        rutaJson = null
        rutaDeEnvio = null
        aviso = null
        modoLibre = false
        sinConexion = false
    }

    private fun sesionVencida() {
        olvidarSesion()
        accesoError = "La sesión venció o se abrió en otro celular. Ingresa de nuevo."
    }

    private fun actualizarCamion(json: JSONObject) {
        camion = Camion.desde(json)
        Sesion.guardarCamion(this, json)
    }

    // ---------------------------------------------------------------
    //  P3-S2 · Viaje: se pregunta al servidor cada 15 s
    // ---------------------------------------------------------------
    private suspend fun consultarViaje(t: String) {
        candado.withLock { consultarSinCandado(t) }
    }

    private suspend fun consultarSinCandado(t: String) {
        if (token != t) return               // se cerró la sesión mientras tanto
        val r = ApiOrbita.viaje(t, conRuta = false)
        if (r is Respuesta.Ok) {
            sinConexion = false
            ultimoErrorConsulta = null
            r.json.optJSONObject("camion")?.let { actualizarCamion(it) }
            val nuevo = r.json.optJSONObject("viaje")?.let { Viaje.desde(it) }
            if (nuevo != null && nuevo.id != rutaDeEnvio) cargarRuta(t, nuevo.id)
            aplicarViaje(nuevo)
            yaConsulto = true
        } else if (r is Respuesta.Error) {
            if (r.codigo == 401) {
                sesionVencida()
            } else if (r.codigo == 0) {
                sinConexion = true
            } else {
                sinConexion = false
                if (r.mensaje != ultimoErrorConsulta) {        // cada error distinto se muestra una sola vez
                    ultimoErrorConsulta = r.mensaje
                    mostrarAviso(r.mensaje, error = true)
                }
            }
        }
    }

    // La línea planeada se pide una sola vez por viaje (es lo más pesado).
    private suspend fun cargarRuta(t: String, idEnvio: Int) {
        val r = ApiOrbita.viaje(t, conRuta = true)
        if (r is Respuesta.Ok) {
            val v = r.json.optJSONObject("viaje")
            val ruta = v?.optJSONObject("ruta")
            if (v != null && v.optInt("id") == idEnvio && ruta != null) {
                rutaJson = ruta.toString()
                rutaDeEnvio = idEnvio
            }
        }
    }

    private fun aplicarViaje(nuevo: Viaje?) {
        val anterior = viaje
        viaje = nuevo
        // Con un viaje asignado se sale de la pantalla de S1 (salvo que esté transmitiendo sin viaje).
        if (nuevo != null && !(EstadoTransmision.activa && EstadoTransmision.envioId == null)) modoLibre = false
        if (nuevo == null) {
            rutaJson = null
            rutaDeEnvio = null
        }

        // Se está transmitiendo un viaje que ya no está activo (lo cancelaron o se terminó): se detiene.
        val enviando = EstadoTransmision.envioId
        if (EstadoTransmision.activa && enviando != null && nuevo?.id != enviando) {
            TransmisionService.detener(this)
            mostrarAviso(
                "El viaje ${String.format(Locale.US, "ENV-%04d", enviando)} ya no está activo: se detuvo la transmisión.",
                error = false
            )
        }

        if (nuevo != null && nuevo.id != anterior?.id && nuevo.estado == "asignado") {
            if (yaConsulto) {
                mostrarAviso(
                    "¡Nuevo viaje asignado! ${nuevo.codigo}: ${nuevo.origenDireccion} → ${nuevo.destinoDireccion}",
                    error = false
                )
            }
            lifecycleScope.launch { latido() }      // el mapa muestra de una vez dónde está el camión
        }
        if (anterior != null && nuevo == null && anterior.estado == "asignado") {
            mostrarAviso("La empresa canceló el viaje ${anterior.codigo}.", error = true)
        }
    }

    // Cada minuto, mientras está disponible, tiene un viaje por empezar o tiene la transmisión del viaje
    // detenida, el camión avisa dónde está. (Transmitiendo no hace falta: la posición llega por UDP cada 5 s.)
    private suspend fun latido() {
        val t = token ?: return
        val c = camion ?: return
        val transmitiendoViaje = EstadoTransmision.activa && EstadoTransmision.envioId != null
        if (c.estado == "no_disponible" || transmitiendoViaje) return
        val u = UbicacionActual.obtener(this) ?: return
        posicionTelefono = u.latitude to u.longitude
        val r = ApiOrbita.ubicacion(t, u.latitude, u.longitude)
        if (r is Respuesta.Error && r.codigo == 401 && token == t) sesionVencida()
    }

    private fun cambiarDisponibilidad(disponible: Boolean) {
        val t = token ?: return
        if (cambiandoDisponibilidad) return
        if (disponible && !tienePermisoUbicacionPrecisa()) {
            pedirPermisos(conBluetooth = false) { cambiarDisponibilidad(true) }
            return
        }
        cambiandoDisponibilidad = true
        lifecycleScope.launch {
            val u = if (disponible) UbicacionActual.obtener(this@MainActivity) else null
            if (disponible && u == null) {
                cambiandoDisponibilidad = false
                mostrarAviso(
                    "No se pudo leer tu ubicación GPS. Revisa que la ubicación del celular esté activada e inténtalo de nuevo.",
                    error = true
                )
                return@launch
            }
            if (u != null) posicionTelefono = u.latitude to u.longitude
            val r = ApiOrbita.disponibilidad(t, disponible, u?.latitude, u?.longitude)
            cambiandoDisponibilidad = false
            when (r) {
                is Respuesta.Ok -> {
                    r.json.optJSONObject("camion")?.let { actualizarCamion(it) }
                    if (disponible) aviso = null
                }
                is Respuesta.Error -> if (r.codigo == 401) sesionVencida() else mostrarAviso(r.mensaje, error = true)
            }
        }
    }

    // Botón radar de las pantallas del viaje: iniciar/reanudar (verificando primero) o cancelar la verificación.
    private fun botonViaje() {
        when {
            fase is FaseVerificacion.EnCurso -> cancelarVerificacion()
            EstadoTransmision.activa && EstadoTransmision.envioId == null ->
                mostrarAviso("Hay una transmisión sin viaje en curso: detenla primero.", error = true)
            EstadoTransmision.activa -> {}
            else -> {
                val v = viaje ?: return
                envioPorIniciar = v
                aviso = null
                pedirPermisos(conBluetooth = true) { prepararVerificacion() }
            }
        }
    }

    private fun finalizarViaje() {
        val t = token ?: return
        val v = viaje ?: return
        if (finalizando) return
        finalizando = true
        lifecycleScope.launch {
            // Con el candado, una consulta en camino no puede volver a poner el viaje ya finalizado.
            val r = candado.withLock { ApiOrbita.finalizar(t) }
            finalizando = false
            when (r) {
                is Respuesta.Ok -> {
                    if (EstadoTransmision.activa) TransmisionService.detener(this@MainActivity)
                    r.json.optJSONObject("camion")?.let { actualizarCamion(it) }
                    viaje = null
                    rutaJson = null
                    rutaDeEnvio = null
                    mostrarAviso("Viaje ${v.codigo} finalizado. Recorriste ${textoKm(v.recorridoM)}. ¡Buen viaje!", error = false)
                }
                is Respuesta.Error -> when {
                    r.codigo == 401 -> sesionVencida()
                    r.codigo == 409 && v.estado == "asignado" -> mostrarAviso(
                        "El servidor todavía no ha recibido tu primera posición del viaje. Espera unos segundos y vuelve a intentarlo.",
                        error = true
                    )
                    else -> mostrarAviso(r.mensaje, error = true)
                }
            }
        }
    }

    // ---------------------------------------------------------------
    //  P3-S1 · Transmisión sin viaje (pantalla de S1)
    // ---------------------------------------------------------------
    // El botón es un interruptor: detiene si está enviando, cancela si está verificando, y si no, empieza.
    private fun alternarEnvio() {
        when {
            EstadoTransmision.activa -> TransmisionService.detener(this)
            fase is FaseVerificacion.EnCurso -> cancelarVerificacion()
            else -> {
                envioPorIniciar = null
                pedirPermisos(conBluetooth = true) { prepararVerificacion() }
            }
        }
    }

    private fun tienePermisoUbicacionPrecisa(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) ==
                PackageManager.PERMISSION_GRANTED

    // Pide los permisos que falten y luego sigue con 'luego' (si se dio la ubicación precisa).
    private fun pedirPermisos(conBluetooth: Boolean, luego: () -> Unit) {
        val necesarios = mutableListOf(
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION   // Android 12+ pide los dos juntos
        )
        // Permiso para mostrar la notificación (solo existe desde Android 13).
        if (Build.VERSION.SDK_INT >= 33) {
            necesarios.add("android.permission.POST_NOTIFICATIONS")
        }
        // P3-S1: permiso de Bluetooth ("Dispositivos cercanos", solo desde Android 12).
        if (conBluetooth) VerificacionConductor.permisoNecesario()?.let { necesarios.add(it) }

        val faltan = necesarios.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }

        if (faltan.isEmpty()) {
            luego()
        } else {
            despuesDePermisos = luego
            permissionLauncher.launch(faltan.toTypedArray())
        }
    }

    // ---------------------------------------------------------------
    //  P3-S1 · Verificación del conductor antes de transmitir
    // ---------------------------------------------------------------
    private fun prepararVerificacion() {
        if (!tienePermisoUbicacionPrecisa()) {
            pedirPermisos(conBluetooth = true) { prepararVerificacion() }
            return
        }
        if (!VerificacionConductor.tienePermiso(this)) {
            fase = FaseVerificacion.Fallida(
                "Falta el permiso de Bluetooth",
                "Sin el permiso de \"Dispositivos cercanos\" la app no puede pedirle al equipo de reconocimiento " +
                        "que reconozca al conductor. Puedes darlo en los ajustes de la app."
            )
            return
        }
        val adaptador = VerificacionConductor.adaptador(this)
        if (adaptador == null) {
            fase = FaseVerificacion.Fallida("Sin Bluetooth", "Este celular no tiene Bluetooth.")
            return
        }
        if (!adaptador.isEnabled) {
            try {
                activarBluetoothLauncher.launch(Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE))
            } catch (e: Exception) {
                fase = FaseVerificacion.Fallida(
                    "Bluetooth apagado",
                    "Actívalo en los ajustes del celular y vuelve a intentarlo."
                )
            }
            return
        }
        val pc = pcElegido
        if (pc == null) {
            abrirSelector(verificarDespues = true)
            return
        }
        verificar(pc)
    }

    private fun verificar(pc: EquipoBluetooth) {
        fase = FaseVerificacion.EnCurso("Conectando con ${pc.nombre}…")
        trabajoVerificacion = lifecycleScope.launch {
            val resultado = VerificacionConductor.identificar(
                this@MainActivity, pc.direccion,
                onAvance = { texto -> fase = FaseVerificacion.EnCurso(texto, (fase as? FaseVerificacion.EnCurso)?.vista) },
                onVista = { vista -> (fase as? FaseVerificacion.EnCurso)?.let { fase = it.copy(vista = vista) } }
            )
            when (resultado) {
                is ResultadoVerificacion.Reconocido -> {
                    fase = FaseVerificacion.Inactiva
                    empezarEnvio(resultado.cedula, resultado.nombre)
                }
                is ResultadoVerificacion.NoReconocido -> {
                    val porque = resultado.motivo?.trim()?.removeSuffix(".")
                        ?: "Ningún conductor registrado coincidió"
                    fase = FaseVerificacion.Fallida(
                        "Conductor no reconocido",
                        "$porque (similitud ${"%.2f".format(resultado.similitud)}). " +
                                "Mira de frente a la cámara, con buena luz, y reintenta."
                    )
                }
                is ResultadoVerificacion.Fallo -> {
                    fase = FaseVerificacion.Fallida("No se pudo verificar", resultado.mensaje)
                }
            }
        }
    }

    private fun cancelarVerificacion() {
        trabajoVerificacion?.cancel()
        VerificacionConductor.cancelar()
        fase = FaseVerificacion.Inactiva
    }

    private fun cambiarComputador() {
        val permiso = VerificacionConductor.permisoNecesario()
        if (permiso != null && !VerificacionConductor.tienePermiso(this)) {
            selectorTrasPermiso = { abrirSelector(verificarDespues = false) }
            permisoBluetoothLauncher.launch(permiso)
        } else {
            abrirSelector(verificarDespues = false)
        }
    }

    // ---------------------------------------------------------------
    //  P3-S3 · ESP32 de sensores de la carga
    // ---------------------------------------------------------------
    private fun cambiarSensores() {
        val permiso = VerificacionConductor.permisoNecesario()
        if (permiso != null && !VerificacionConductor.tienePermiso(this)) {
            selectorTrasPermiso = { cambiarSensores() }
            permisoBluetoothLauncher.launch(permiso)
            return
        }
        equiposVinculados = VerificacionConductor.equiposVinculados(this)
        mostrarSelectorSensores = true
    }

    private fun elegirSensores(equipo: EquipoBluetooth?) {
        Sensores.guardar(this, equipo)
        sensoresElegido = equipo
        mostrarSelectorSensores = false
        // En pleno viaje: el servicio se conecta (o se desconecta) de una vez.
        if (EstadoTransmision.activa && EstadoTransmision.envioId != null) {
            TransmisionService.reconectarSensores(this)
        }
        mostrarAviso(
            if (equipo == null) "Sin sensores de la carga: el viaje funciona igual, solo con el GPS."
            else "Sensores de la carga: ${equipo.nombre}. Se conectan al iniciar el viaje.",
            error = false
        )
    }

    private fun abrirSelector(verificarDespues: Boolean) {
        equiposVinculados = VerificacionConductor.equiposVinculados(this)
        verificarAlElegir = verificarDespues
        mostrarSelector = true
    }

    private fun elegirPc(equipo: EquipoBluetooth) {
        VerificacionConductor.guardarPc(this, equipo)
        pcElegido = equipo
        mostrarSelector = false
        if (verificarAlElegir) {
            verificarAlElegir = false
            verificar(equipo)
        }
    }

    // ---------------------------------------------------------------
    //  Transmisión (con viaje o sin él)
    // ---------------------------------------------------------------
    private fun empezarEnvio(cedula: String?, nombre: String?) {
        val porIniciar = envioPorIniciar
        envioPorIniciar = null
        fase = FaseVerificacion.Inactiva
        // P3-S2: si el viaje cambió mientras se verificaba (por ejemplo, lo cancelaron), no se arranca.
        if (porIniciar != null && viaje?.id != porIniciar.id) {
            mostrarAviso("El viaje ${porIniciar.codigo} ya no está asignado a este camión.", error = true)
            return
        }
        EstadoTransmision.hayError = false
        EstadoTransmision.mensaje = "Iniciando…"
        EstadoTransmision.activa = true      // el botón cambia de inmediato; el servicio lo confirma (o lo revierte)
        EstadoTransmision.envioId = porIniciar?.id
        try {
            TransmisionService.iniciar(
                this, cedula, nombre,
                idEnvio = porIniciar?.id,
                recorridoPrevio = porIniciar?.idRecorrido?.toLongOrNull()   // al reanudar: el mismo recorrido
            )
        } catch (e: Exception) {
            EstadoTransmision.activa = false
            EstadoTransmision.envioId = null
            EstadoTransmision.hayError = true
            EstadoTransmision.mensaje = "No se pudo iniciar el envío: ${e.message}"
            mostrarAviso(EstadoTransmision.mensaje, error = true)
            return
        }
        pedirNoOptimizarBateria()
    }

    // Muchos celulares (Xiaomi/MIUI, Samsung, etc.) cierran las apps en segundo plano para
    // ahorrar batería. Se pide UNA sola vez que esta app quede exenta; si el usuario acepta,
    // el envío sobrevive mucho mejor con la pantalla bloqueada.
    private fun pedirNoOptimizarBateria() {
        val prefs = getSharedPreferences("rastreo_prefs", MODE_PRIVATE)
        if (prefs.getBoolean("pidio_bateria", false)) return

        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) return

        prefs.edit().putBoolean("pidio_bateria", true).apply()
        try {
            startActivity(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
                    .setData(Uri.parse("package:$packageName"))
            )
        } catch (e: Exception) {
            // Algunos teléfonos no tienen esta pantalla: se ignora, la app funciona igual.
        }
    }
}

@Composable
fun OrbitaTema(contenido: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = darkColorScheme(
            primary = AzulMarca,
            onPrimary = Color.White,
            background = Oscuro,
            surface = Medio,
            onSurface = Claro
        ),
        content = contenido
    )
}

@Composable
fun PantallaPrincipal(
    activa: Boolean,
    mensaje: String,
    hayError: Boolean,
    enviados: Int,
    ultimoEnvio: String,
    notificacionesActivas: Boolean,
    fase: FaseVerificacion,
    conductorNombre: String?,
    conductorCedula: String?,
    pcNombre: String?,
    onEnviarClick: () -> Unit,
    onReintentar: () -> Unit,
    onSinVerificar: () -> Unit,
    onCambiarPc: () -> Unit,
    avisoViaje: String? = null,            // P3-S2: hay un viaje asignado mientras se transmite sin viaje
    onVolver: (() -> Unit)? = null         // P3-S2: volver al panel del camión
) {
    // Buscando GPS = activa pero todavía sin ningún envío ni error.
    val buscando = activa && !hayError && enviados == 0
    val verificando = fase is FaseVerificacion.EnCurso

    val titulo = when {
        activa && hayError -> "Algo no está bien"
        activa && buscando -> "Buscando señal GPS"
        activa -> "Transmitiendo en vivo"
        fase is FaseVerificacion.EnCurso -> "Verificando conductor"
        fase is FaseVerificacion.Fallida -> fase.titulo
        hayError -> "Algo no está bien"
        else -> "Listo para transmitir"
    }
    val subtitulo = when {
        activa -> "Toca el botón para detener el envío"
        fase is FaseVerificacion.EnCurso -> fase.texto
        fase is FaseVerificacion.Fallida -> "Puedes reintentar o transmitir sin verificar"
        else -> "Toca el botón: primero se verifica al conductor"
    }
    val tituloEnAlerta = (activa && hayError) || (!activa && (fase is FaseVerificacion.Fallida || hayError))
    val estadoBoton = when {
        activa -> EstadoBoton.ACTIVO
        verificando -> EstadoBoton.VERIFICANDO
        else -> EstadoBoton.REPOSO
    }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Brush.verticalGradient(listOf(Oscuro, FondoAbajo)))
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .systemBarsPadding()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 24.dp, vertical = 16.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center
        ) {
            Image(
                painter = painterResource(R.drawable.orbita_logo),
                contentDescription = "Orbita. Cada kilómetro bajo tu radar",
                modifier = Modifier
                    .width(260.dp)
                    .aspectRatio(714f / 254f)
            )

            Spacer(modifier = Modifier.height(12.dp))

            if (avisoViaje != null) {
                Text(
                    text = avisoViaje,
                    fontFamily = Manrope,
                    fontWeight = FontWeight.ExtraBold,
                    fontSize = 14.sp,
                    color = Ambar,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.padding(bottom = 8.dp)
                )
            }

            BotonRadar(estado = estadoBoton, onClick = onEnviarClick)

            Spacer(modifier = Modifier.height(8.dp))

            Text(
                text = titulo,
                fontFamily = Manrope,
                fontWeight = FontWeight.ExtraBold,
                fontSize = 24.sp,
                color = if (tituloEnAlerta) NaranjaMarca else Claro,
                textAlign = TextAlign.Center
            )
            Text(
                text = subtitulo,
                fontFamily = Manrope,
                fontWeight = FontWeight.SemiBold,
                fontSize = 14.sp,
                color = GrisAzul,
                textAlign = TextAlign.Center,
                modifier = Modifier.padding(top = 4.dp, bottom = 24.dp)
            )

            when {
                activa -> {
                    TarjetaConductor(nombre = conductorNombre, cedula = conductorCedula)
                    Spacer(modifier = Modifier.height(12.dp))
                    TarjetaDeEstado(
                        activa = activa,
                        mensaje = mensaje,
                        hayError = hayError,
                        enviados = enviados,
                        ultimoEnvio = ultimoEnvio
                    )
                }
                fase is FaseVerificacion.Fallida -> {
                    TarjetaVerificacionFallida(
                        detalle = fase.detalle,
                        onReintentar = onReintentar,
                        onSinVerificar = onSinVerificar
                    )
                }
                verificando -> {
                    Text(
                        text = "La cámara del equipo de reconocimiento debe ver tu cara de frente y con buena luz. " +
                                "Toca el botón para cancelar.",
                        fontFamily = Manrope,
                        fontWeight = FontWeight.SemiBold,
                        fontSize = 13.sp,
                        color = GrisAzul,
                        textAlign = TextAlign.Center
                    )
                }
                else -> {
                    TarjetaDeEstado(
                        activa = activa,
                        mensaje = mensaje,
                        hayError = hayError,
                        enviados = enviados,
                        ultimoEnvio = ultimoEnvio
                    )
                }
            }

            Spacer(modifier = Modifier.height(16.dp))

            if (activa) {
                Text(
                    text = "Puedes bloquear el celular o cambiar de aplicación: el envío sigue " +
                            "funcionando en segundo plano. Mientras esté activo verás una notificación.",
                    fontFamily = Manrope,
                    fontWeight = FontWeight.SemiBold,
                    fontSize = 12.sp,
                    color = GrisAzul,
                    textAlign = TextAlign.Center
                )
                if (!notificacionesActivas) {
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "Las notificaciones están desactivadas para esta app. " +
                                "Actívalas en los ajustes del teléfono para ver el aviso.",
                        fontFamily = Manrope,
                        fontWeight = FontWeight.SemiBold,
                        fontSize = 12.sp,
                        color = NaranjaMarca,
                        textAlign = TextAlign.Center
                    )
                }
                Spacer(modifier = Modifier.height(8.dp))
            } else if (!verificando) {
                // Computador de reconocimiento elegido (se puede cambiar).
                Text(
                    text = "Equipo de reconocimiento (computador o ESP32-CAM): ${pcNombre ?: "sin elegir"}  ·  Cambiar",
                    fontFamily = Manrope,
                    fontWeight = FontWeight.SemiBold,
                    fontSize = 12.sp,
                    color = CianMarca,
                    textAlign = TextAlign.Center,
                    modifier = Modifier
                        .clip(RoundedCornerShape(8.dp))
                        .clickable(onClick = onCambiarPc)
                        .padding(horizontal = 8.dp, vertical = 6.dp)
                )
                Spacer(modifier = Modifier.height(4.dp))
            }

            Text(
                text = "Servidor ${Config.IP_SERVIDOR}:${Config.PUERTO} · cada ${Config.INTERVALO_MS / 1000} s",
                fontFamily = Manrope,
                fontWeight = FontWeight.SemiBold,
                fontSize = 11.sp,
                color = GrisAzul.copy(alpha = 0.65f)
            )

            if (onVolver != null) {
                Spacer(modifier = Modifier.height(12.dp))
                Text(
                    text = "← Volver al panel del camión",
                    fontFamily = Manrope,
                    fontWeight = FontWeight.ExtraBold,
                    fontSize = 14.sp,
                    color = CianMarca,
                    modifier = Modifier
                        .clip(RoundedCornerShape(8.dp))
                        .clickable(onClick = onVolver)
                        .padding(horizontal = 10.dp, vertical = 8.dp)
                )
            }
        }
    }
}

/**
 * El botón principal, con aspecto de radar:
 *   REPOSO      botón cian con ▶
 *   VERIFICANDO botón oscuro con un círculo girando y ondas ámbar (tocar = cancelar)
 *   ACTIVO      botón oscuro con ■ naranja y ondas cian
 */
@Composable
internal fun BotonRadar(estado: EstadoBoton, onClick: () -> Unit, tamano: Dp = 270.dp) {
    val descripcion = when (estado) {
        EstadoBoton.REPOSO -> "Iniciar: verificar conductor y enviar ubicación"
        EstadoBoton.VERIFICANDO -> "Cancelar la verificación"
        EstadoBoton.ACTIVO -> "Detener envío de ubicación"
    }
    val reposo = estado == EstadoBoton.REPOSO

    val interior = tamano * 0.578f        // el botón ocupa lo mismo que en S1 (156 de 270)
    Box(
        modifier = Modifier.size(tamano),
        contentAlignment = Alignment.Center
    ) {
        // Resplandor azul y anillos tenues de fondo.
        Canvas(modifier = Modifier.fillMaxSize()) {
            val radio = size.minDimension / 2f
            drawCircle(
                brush = Brush.radialGradient(
                    colors = listOf(AzulMarca.copy(alpha = if (reposo) 0.30f else 0.22f), Color.Transparent),
                    radius = radio
                ),
                radius = radio
            )
            drawCircle(
                color = CianMarca.copy(alpha = 0.10f),
                radius = radio * 0.80f,
                style = Stroke(width = 1.dp.toPx())
            )
            drawCircle(
                color = CianMarca.copy(alpha = 0.06f),
                radius = radio * 0.98f,
                style = Stroke(width = 1.dp.toPx())
            )
        }

        // Ondas que salen del botón.
        when (estado) {
            EstadoBoton.ACTIVO -> PulsosRadar(color = CianMarca, modifier = Modifier.fillMaxSize())
            EstadoBoton.VERIFICANDO -> PulsosRadar(color = Ambar, modifier = Modifier.fillMaxSize())
            EstadoBoton.REPOSO -> {}
        }

        Box(
            modifier = Modifier
                .size(interior)
                .clip(CircleShape)
                .background(
                    if (reposo) {
                        Brush.linearGradient(listOf(CianMarca, AzulMarca))
                    } else {
                        Brush.radialGradient(listOf(Medio, Oscuro))
                    }
                )
                .border(
                    width = 2.dp,
                    color = when (estado) {
                        EstadoBoton.REPOSO -> Color.White.copy(alpha = 0.35f)
                        EstadoBoton.VERIFICANDO -> Ambar
                        EstadoBoton.ACTIVO -> CianMarca
                    },
                    shape = CircleShape
                )
                .semantics { contentDescription = descripcion }
                .clickable(onClick = onClick),
            contentAlignment = Alignment.Center
        ) {
            if (estado == EstadoBoton.VERIFICANDO) {
                CircularProgressIndicator(
                    modifier = Modifier.size(interior * 0.37f),
                    strokeWidth = 4.dp,
                    color = Ambar
                )
            } else {
                Canvas(modifier = Modifier.size(interior * 0.333f)) {
                    if (estado == EstadoBoton.ACTIVO) dibujarStop(NaranjaMarca) else dibujarPlay(Oscuro)
                }
            }
        }
    }
}

// Tres anillos que se expanden y se desvanecen, uno detrás de otro.
@Composable
internal fun PulsosRadar(color: Color, modifier: Modifier = Modifier) {
    val transicion = rememberInfiniteTransition()
    val fase by transicion.animateFloat(
        initialValue = 0f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 3000, easing = LinearEasing)
        )
    )

    Canvas(modifier = modifier) {
        val radioMax = size.minDimension / 2f
        val radioMin = radioMax * 0.60f          // justo en el borde del botón
        for (i in 0 until 3) {
            val avance = (fase + i / 3f) % 1f
            drawCircle(
                color = color.copy(alpha = (1f - avance) * 0.55f),
                radius = radioMin + (radioMax - radioMin) * avance,
                style = Stroke(width = 2.dp.toPx())
            )
        }
    }
}

private fun DrawScope.dibujarPlay(color: Color) {
    val ancho = size.width
    val alto = size.height
    val triangulo = Path().apply {
        moveTo(ancho * 0.28f, alto * 0.14f)
        lineTo(ancho * 0.28f, alto * 0.86f)
        lineTo(ancho * 0.88f, alto * 0.50f)
        close()
    }
    drawPath(triangulo, color)
    // El trazo con unión redondeada suaviza las esquinas.
    drawPath(triangulo, color, style = Stroke(width = 6.dp.toPx(), join = StrokeJoin.Round))
}

private fun DrawScope.dibujarStop(color: Color) {
    val lado = size.width * 0.62f
    drawRoundRect(
        color = color,
        topLeft = Offset((size.width - lado) / 2f, (size.height - lado) / 2f),
        size = Size(lado, lado),
        cornerRadius = CornerRadius(8.dp.toPx())
    )
}

// '1001000001' -> '1.001.000.001'
private fun formatearCedula(cedula: String): String =
    cedula.reversed().chunked(3).joinToString(".").reversed()

/** P3-S1: conductor del recorrido en curso (o aviso de que va sin verificar). */
@Composable
internal fun TarjetaConductor(nombre: String?, cedula: String?) {
    val verificado = nombre != null
    Card(
        shape = RoundedCornerShape(18.dp),
        colors = CardDefaults.cardColors(containerColor = Medio.copy(alpha = 0.55f)),
        border = BorderStroke(1.dp, (if (verificado) CianMarca else GrisAzul).copy(alpha = 0.45f)),
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(
                text = if (verificado) "CONDUCTOR VERIFICADO" else "CONDUCTOR",
                fontFamily = Manrope,
                fontWeight = FontWeight.SemiBold,
                fontSize = 10.sp,
                letterSpacing = 1.sp,
                color = if (verificado) CianMarca else GrisAzul
            )
            Text(
                text = nombre ?: "Sin verificar (planilla en blanco)",
                fontFamily = Manrope,
                fontWeight = FontWeight.ExtraBold,
                fontSize = if (verificado) 18.sp else 15.sp,
                color = Claro,
                modifier = Modifier.padding(top = 2.dp)
            )
            if (cedula != null) {
                Text(
                    text = "C.C. ${formatearCedula(cedula)}",
                    fontFamily = Manrope,
                    fontWeight = FontWeight.SemiBold,
                    fontSize = 13.sp,
                    color = GrisAzul,
                    modifier = Modifier.padding(top = 2.dp)
                )
            }
        }
    }
}

/** P3-S1: la verificación no salió bien — reintentar o transmitir sin verificar. */
@Composable
internal fun TarjetaVerificacionFallida(detalle: String, onReintentar: () -> Unit, onSinVerificar: () -> Unit) {
    Card(
        shape = RoundedCornerShape(18.dp),
        colors = CardDefaults.cardColors(containerColor = Medio.copy(alpha = 0.55f)),
        border = BorderStroke(1.dp, NaranjaMarca.copy(alpha = 0.5f)),
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(
                text = detalle,
                fontFamily = Manrope,
                fontWeight = FontWeight.SemiBold,
                fontSize = 14.sp,
                color = Claro
            )
            Spacer(modifier = Modifier.height(14.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(
                    onClick = onReintentar,
                    colors = ButtonDefaults.buttonColors(containerColor = AzulMarca, contentColor = Color.White),
                    modifier = Modifier.weight(1f)
                ) {
                    Text("Reintentar", fontFamily = Manrope, fontWeight = FontWeight.ExtraBold, fontSize = 13.sp)
                }
                OutlinedButton(
                    onClick = onSinVerificar,
                    border = BorderStroke(1.dp, GrisAzul),
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = Claro),
                    modifier = Modifier.weight(1f)
                ) {
                    Text(
                        "Sin verificar",
                        fontFamily = Manrope,
                        fontWeight = FontWeight.ExtraBold,
                        fontSize = 13.sp,
                        textAlign = TextAlign.Center
                    )
                }
            }
            Text(
                text = "\"Sin verificar\" transmite igual, pero el recorrido queda con la planilla en blanco.",
                fontFamily = Manrope,
                fontWeight = FontWeight.SemiBold,
                fontSize = 11.sp,
                color = GrisAzul,
                modifier = Modifier.padding(top = 10.dp)
            )
        }
    }
}

/**
 * Lista de equipos vinculados por Bluetooth para elegir uno.
 * P3-S1: el equipo de reconocimiento (computador o ESP32-CAM). P3-S3: también el ESP32 de sensores.
 */
@Composable
private fun SelectorDeComputador(
    equipos: List<EquipoBluetooth>,
    onElegir: (EquipoBluetooth) -> Unit,
    onCerrar: () -> Unit,
    titulo: String = "Elige el equipo de reconocimiento",
    ayuda: String = "Equipos vinculados por Bluetooth. Elige el computador (o ESP32-CAM) que tiene corriendo " +
            "el reconocimiento facial.",
    sinEquipos: String = "No hay equipos vinculados. En los ajustes de Bluetooth del celular, vincula el " +
            "equipo de reconocimiento (computador o ESP32-CAM) y vuelve a intentarlo.",
    textoQuitar: String? = null,
    onQuitar: () -> Unit = {}
) {
    AlertDialog(
        onDismissRequest = onCerrar,
        confirmButton = {
            TextButton(onClick = onCerrar) {
                Text("Cancelar", fontFamily = Manrope, fontWeight = FontWeight.ExtraBold, color = CianMarca)
            }
        },
        dismissButton = if (textoQuitar != null) {
            {
                TextButton(onClick = onQuitar) {
                    Text(textoQuitar, fontFamily = Manrope, fontWeight = FontWeight.ExtraBold, color = NaranjaMarca)
                }
            }
        } else null,
        title = {
            Text(titulo, fontFamily = Manrope, fontWeight = FontWeight.ExtraBold)
        },
        text = {
            if (equipos.isEmpty()) {
                Text(
                    text = sinEquipos,
                    fontFamily = Manrope,
                    fontWeight = FontWeight.SemiBold,
                    fontSize = 14.sp
                )
            } else {
                Column(
                    modifier = Modifier
                        .heightIn(max = 360.dp)
                        .verticalScroll(rememberScrollState())
                ) {
                    Text(
                        text = ayuda,
                        fontFamily = Manrope,
                        fontWeight = FontWeight.SemiBold,
                        fontSize = 13.sp,
                        color = GrisAzul
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    equipos.forEach { equipo ->
                        Column(
                            modifier = Modifier
                                .fillMaxWidth()
                                .clip(RoundedCornerShape(12.dp))
                                .clickable { onElegir(equipo) }
                                .padding(horizontal = 10.dp, vertical = 10.dp)
                        ) {
                            Text(equipo.nombre, fontFamily = Manrope, fontWeight = FontWeight.ExtraBold, fontSize = 15.sp, color = Claro)
                            Text(equipo.direccion, fontFamily = Manrope, fontWeight = FontWeight.SemiBold, fontSize = 11.sp, color = GrisAzul)
                        }
                    }
                }
            }
        },
        containerColor = Oscuro,
        titleContentColor = Claro,
        textContentColor = Claro
    )
}

@Composable
fun TarjetaDeEstado(
    activa: Boolean,
    mensaje: String,
    hayError: Boolean,
    enviados: Int,
    ultimoEnvio: String
) {
    val buscando = activa && !hayError && enviados == 0
    val acento = when {
        hayError -> NaranjaMarca
        buscando -> Ambar
        activa -> CianMarca
        else -> GrisAzul
    }

    Card(
        shape = RoundedCornerShape(18.dp),
        colors = CardDefaults.cardColors(containerColor = Medio.copy(alpha = 0.55f)),
        border = BorderStroke(1.dp, acento.copy(alpha = 0.45f)),
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (buscando) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(16.dp),
                        strokeWidth = 2.dp,
                        color = acento
                    )
                } else {
                    Box(
                        modifier = Modifier
                            .size(10.dp)
                            .clip(CircleShape)
                            .background(acento)
                    )
                }
                Spacer(modifier = Modifier.width(10.dp))
                Text(
                    text = mensaje,
                    fontFamily = Manrope,
                    fontWeight = FontWeight.SemiBold,
                    fontSize = 14.sp,
                    color = Claro
                )
            }

            if (activa && enviados > 0) {
                Spacer(modifier = Modifier.height(14.dp))
                Row(modifier = Modifier.fillMaxWidth()) {
                    Dato(etiqueta = "ENVIADAS", valor = enviados.toString(), modifier = Modifier.weight(1f))
                    Dato(etiqueta = "ÚLTIMO ENVÍO", valor = ultimoEnvio, modifier = Modifier.weight(1f))
                }
            }
        }
    }
}

@Composable
internal fun Dato(etiqueta: String, valor: String, modifier: Modifier = Modifier) {
    Column(modifier = modifier) {
        Text(
            text = etiqueta,
            fontFamily = Manrope,
            fontWeight = FontWeight.SemiBold,
            fontSize = 10.sp,
            letterSpacing = 1.sp,
            color = GrisAzul
        )
        Text(
            text = valor,
            fontFamily = Manrope,
            fontWeight = FontWeight.ExtraBold,
            fontSize = 20.sp,
            color = Claro,
            modifier = Modifier.padding(top = 2.dp)
        )
    }
}