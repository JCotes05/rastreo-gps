package com.tunombre.localizadorsms

import android.Manifest
import android.app.Activity
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Telephony
import android.telephony.PhoneNumberUtils
import android.telephony.SmsManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.LocationOn
import androidx.compose.material.icons.filled.Send
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

// colores de la app
private val AzulPrincipal = Color(0xFF3D5AFE)
private val AzulOscuro = Color(0xFF1A237E)
private val FondoClaro = Color(0xFFF4F6FF)
private val GrisTexto = Color(0xFF5C6270)
private val FondoCargando = Color(0xFFFFF3CD)
private val FondoExito = Color(0xFFE0F8E9)
private val FondoError = Color(0xFFFDE0E0)
private val FondoIncierto = Color(0xFFFFE9C7)

sealed class EstadoApp {
    object Esperando : EstadoApp()
    data class Cargando(val mensaje: String) : EstadoApp()
    data class Exito(val mensaje: String) : EstadoApp()
    data class Error(val mensaje: String) : EstadoApp()
    data class Incierto(val mensaje: String) : EstadoApp() // no hubo confirmación pero probablemente sí se envió
}

// Representa el resultado de UN intento de envío (una combinación protocolo + destino).
// La UI usa esto para mostrar una pequeña "bitácora" con lo que pasó en cada uno.
data class ResultadoEnvio(
    val protocolo: String,   // "UDP"
    val destino: String,     // ej. "192.168.1.50:5000"
    val exito: Boolean,
    val detalle: String
)

class MainActivity : ComponentActivity() {

    private lateinit var fusedLocationClient: FusedLocationProviderClient
    private var estado by mutableStateOf<EstadoApp>(EstadoApp.Esperando)
    private var numeroDestino: String = ""

    // --- Estado y datos para el envío por RED (UDP a las 2 casas) ---
    private var estadoRed by mutableStateOf("Listo para enviar por red")
    private val resultadosRed = mutableStateListOf<ResultadoEnvio>()

    private val handlerTimeout = Handler(Looper.getMainLooper())
    private var timeoutRunnable: Runnable? = null

    // verificación de respaldo contra la bandeja de Enviados (por lo de MIUI)
    private val handlerVerificacion = Handler(Looper.getMainLooper())
    private var verificacionRunnable: Runnable? = null
    private var resultadoYaResuelto = false

    // --- Transmisión continua por red: repite el envío lo más rápido posible ---
    private val handlerTransmision = Handler(Looper.getMainLooper())
    private var runnableTransmision: Runnable? = null
    private var transmisionActiva by mutableStateOf(false)
    private val intervaloTransmisionMs = 5_000L // 5 segundos — le da tiempo real al GPS entre lecturas

    // Evita que 2 solicitudes de ubicación se sobrepongan si el GPS tarda
    // más de 1 segundo en responder alguna vez — sin esto, podríamos
    // terminar con 2 "hilos" de envío corriendo a la vez, en desorden.
    private var solicitudEnCurso = false

    // Identifica UN recorrido completo: se genera un valor nuevo cada vez
    // que se presiona "Iniciar", y viaja dentro de CADA mensaje UDP de ese
    // recorrido. Así el servidor sabe qué puntos pertenecen a un mismo
    // trayecto, sin necesitar un mensaje aparte de "inicio" o "fin".
    private var idRecorridoActual: Long = 0L

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { permisos ->
        val ubicacionOk = permisos[Manifest.permission.ACCESS_FINE_LOCATION] == true
        val smsOk = permisos[Manifest.permission.SEND_SMS] == true

        if (ubicacionOk && smsOk) {
            obtenerUbicacionYEnviar()
        } else {
            estado = EstadoApp.Error("Necesitamos permiso de Ubicación y SMS para poder funcionar")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        fusedLocationClient = LocationServices.getFusedLocationProviderClient(this)

        setContent {
            PantallaPrincipal(
                estado = estado,
                onEnviarClick = { numero ->
                    numeroDestino = numero
                    verificarPermisosYEmpezar()
                },
                estadoRed = estadoRed,
                resultadosRed = resultadosRed,
                transmisionActiva = transmisionActiva,
                onEnviarRedClick = { ip ->
                    alternarTransmisionContinua(ip)
                }
            )
        }
    }

    private fun verificarPermisosYEmpezar() {
        if (numeroDestino.isBlank()) {
            estado = EstadoApp.Error("Escribe primero un número de teléfono válido")
            return
        }

        val permisosNecesarios = arrayOf(
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.SEND_SMS,
            Manifest.permission.READ_SMS
        )

        val faltanPermisos = permisosNecesarios.any {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }

        if (faltanPermisos) {
            permissionLauncher.launch(permisosNecesarios)
        } else {
            obtenerUbicacionYEnviar()
        }
    }

    private fun obtenerUbicacionYEnviar() {
        estado = EstadoApp.Cargando("Buscando tu ubicación GPS...")

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
            != PackageManager.PERMISSION_GRANTED
        ) {
            estado = EstadoApp.Error("Falta el permiso de ubicación")
            return
        }

        fusedLocationClient.getCurrentLocation(Priority.PRIORITY_HIGH_ACCURACY, null)
            .addOnSuccessListener { ubicacion ->
                if (ubicacion != null) {
                    val lat = "%.5f".format(ubicacion.latitude)
                    val lon = "%.5f".format(ubicacion.longitude)
                    // ubicacion.time = hora que marcó el GPS al tomar la lectura (no la del sistema Android)
                    val horaGps = SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date(ubicacion.time))
                    // solo coordenadas + hora del GPS)
                    val mensaje = "Ubicacion: Lat:$lat,Lon:$lon\nHora:$horaGps"
                    enviarSms(numeroDestino, mensaje)
                } else {
                    estado = EstadoApp.Error("No se pudo obtener la ubicación. ¿Tienes el GPS activado?")
                }
            }
            .addOnFailureListener { error ->
                estado = EstadoApp.Error("Error al obtener la ubicación: ${error.message}")
            }
    }

    private fun enviarSms(numero: String, mensaje: String) {
        estado = EstadoApp.Cargando("Enviando el SMS...")
        resultadoYaResuelto = false

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.SEND_SMS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            estado = EstadoApp.Error("Falta el permiso de SMS")
            return
        }

        try {
            val smsManager = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                getSystemService(SmsManager::class.java)
            } else {
                @Suppress("DEPRECATION")
                SmsManager.getDefault()
            }

            // receiver para el resultado del envío (a veces MIUI no lo dispara, por eso el backup de abajo)
            val receiver = object : BroadcastReceiver() {
                override fun onReceive(context: Context, intent: Intent) {
                    if (resultadoYaResuelto) return
                    resultadoYaResuelto = true
                    cancelarTimeout()
                    cancelarVerificacion()

                    estado = when (resultCode) {
                        Activity.RESULT_OK ->
                            EstadoApp.Exito("¡Ubicación enviada con éxito!")
                        SmsManager.RESULT_ERROR_NO_SERVICE ->
                            EstadoApp.Error("Sin señal / sin servicio de red. Revisa la cobertura del SIM.")
                        SmsManager.RESULT_ERROR_RADIO_OFF ->
                            EstadoApp.Error("El radio del teléfono está apagado (¿modo avión?)")
                        SmsManager.RESULT_ERROR_NULL_PDU ->
                            EstadoApp.Error("Error interno al construir el mensaje.")
                        SmsManager.RESULT_ERROR_GENERIC_FAILURE ->
                            EstadoApp.Error("Fallo genérico. Revisa permisos de SMS y el saldo/plan del SIM.")
                        else ->
                            EstadoApp.Error("Error desconocido (código $resultCode)")
                    }
                    try {
                        unregisterReceiver(this)
                    } catch (e: IllegalArgumentException) {
                    }
                }
            }

            val accionEnviado = "com.tuempresa.localizadorsms.SMS_ENVIADO"

            ContextCompat.registerReceiver(
                this,
                receiver,
                IntentFilter(accionEnviado),
                ContextCompat.RECEIVER_NOT_EXPORTED
            )

            val sentPendingIntent = PendingIntent.getBroadcast(
                this,
                0,
                Intent(accionEnviado),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )

            val horaDeEnvio = System.currentTimeMillis()

            // backup: revisa la bandeja de Enviados por si el broadcast no llega (pasa en Xiaomi)
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.READ_SMS)
                == PackageManager.PERMISSION_GRANTED
            ) {
                iniciarVerificacionPorProveedor(numero, mensaje, horaDeEnvio, receiver)
            }

            cancelarTimeout()
            timeoutRunnable = Runnable {
                if (!resultadoYaResuelto) {
                    resultadoYaResuelto = true
                    cancelarVerificacion()
                    try {
                        unregisterReceiver(receiver)
                    } catch (e: IllegalArgumentException) {
                    }
                    estado = EstadoApp.Incierto(
                        "El SMS probablemente SÍ se envió, pero el sistema no confirmó a tiempo. " +
                                "Revisa el chat del destinatario."
                    )
                }
            }
            handlerTimeout.postDelayed(timeoutRunnable!!, 20000)

            smsManager.sendTextMessage(numero, null, mensaje, sentPendingIntent, null)
        } catch (e: Exception) {
            cancelarTimeout()
            cancelarVerificacion()
            estado = EstadoApp.Error("No se pudo enviar el SMS: ${e.message}")
        }
    }

    // ================================================================
    //  FASE 2 (P1-S2): ENVÍO POR RED — UDP A LAS 2 CASAS
    // ================================================================

    // Igual que obtenerUbicacionYEnviar(), pero en vez de mandar por SMS,
    // manda el mismo texto por UDP a cada una de las 2 direcciones.
    // ================================================================
    //  TRANSMISIÓN CONTINUA: repite el envío cada 10s hasta pausar
    // ================================================================

    // Esto es lo que conecta el botón: si ya está transmitiendo, lo
    // detiene; si no, lo arranca. Es un "interruptor" (toggle).
    private fun alternarTransmisionContinua(direccionIP: String) {
        if (transmisionActiva) {
            detenerTransmisionContinua()
        } else {
            iniciarTransmisionContinua(direccionIP)
        }
    }

    private fun iniciarTransmisionContinua(direccionIP: String) {
        transmisionActiva = true
        solicitudEnCurso = false
        // Nuevo recorrido = nuevo ID. Todo lo que se mande desde aquí
        // hasta que se pause pertenece a ESTE recorrido específico.
        idRecorridoActual = System.currentTimeMillis()
        estadoRed = "Recorrido iniciado (ID $idRecorridoActual)"

        val direccionCompleta = "$direccionIP:5000" // el puerto siempre es 5000, fijo

        // Este Runnable hace algo importante: cada vez que se ejecuta,
        // programa su PROPIA siguiente ejecución después. Así se logra
        // "repetir" sin necesitar un bucle "while" que bloquearía el hilo.
        runnableTransmision = object : Runnable {
            override fun run() {
                if (!transmisionActiva) return // seguridad: si justo se pausó, no sigas
                // Si la solicitud anterior TODAVÍA no ha respondido, nos
                // saltamos este ciclo — evita 2 lecturas de GPS a la vez
                // pisándose una a la otra.
                if (!solicitudEnCurso) {
                    obtenerUbicacionYEnviarPorRed(direccionCompleta)
                }
                handlerTransmision.postDelayed(this, intervaloTransmisionMs)
            }
        }
        // La primera vez se ejecuta DE INMEDIATO (sin esperar el primer ciclo).
        handlerTransmision.post(runnableTransmision!!)
    }

    private fun detenerTransmisionContinua() {
        transmisionActiva = false
        runnableTransmision?.let { handlerTransmision.removeCallbacks(it) }
        runnableTransmision = null
        estadoRed = "Fin del recorrido (ID $idRecorridoActual)"
    }

    private fun obtenerUbicacionYEnviarPorRed(direccionCompleta: String) {
        solicitudEnCurso = true
        resultadosRed.clear() // sin esto, con 1 envío por segundo la lista crecería sin límite

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
            != PackageManager.PERMISSION_GRANTED
        ) {
            estadoRed = "Falta el permiso de ubicación"
            solicitudEnCurso = false
            return
        }

        fusedLocationClient.getCurrentLocation(Priority.PRIORITY_HIGH_ACCURACY, null)
            .addOnSuccessListener { ubicacion ->
                if (ubicacion != null) {
                    val lat = "%.5f".format(ubicacion.latitude)
                    val lon = "%.5f".format(ubicacion.longitude)
                    val horaGps = SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date(ubicacion.time))
                    val mensaje = "LAT:$lat,LON:$lon,HORA:$horaGps,RECORRIDO:$idRecorridoActual"
                    enviarATodasLasCasas(mensaje, listOf(direccionCompleta))
                } else {
                    estadoRed = "No se pudo obtener la ubicación. ¿Tienes el GPS activado?"
                }
                solicitudEnCurso = false
            }
            .addOnFailureListener { error ->
                estadoRed = "Error al obtener la ubicación: ${error.message}"
                solicitudEnCurso = false
            }
    }

    // Recorre las direcciones (las 2 casas) y, para CADA UNA, manda el
    // mensaje por UDP. Usamos una corrutina porque hacer red en el hilo
    // principal está prohibido en Android (se cae la app).
    private fun enviarATodasLasCasas(mensaje: String, direcciones: List<String>) {
        estadoRed = "Enviando por UDP a ${direcciones.size} casa(s)..."

        lifecycleScope.launch {
            for (direccionCompleta in direcciones) {
                // Cada dirección se escribe como "ip:puerto", ej: 192.168.1.50:5000
                val partes = direccionCompleta.trim().split(":")
                if (partes.size != 2) {
                    resultadosRed.add(
                        ResultadoEnvio("—", direccionCompleta, false, "Formato inválido: usa ip:puerto")
                    )
                    continue
                }
                val ip = partes[0]
                val puerto = partes[1].toIntOrNull()
                if (ip.isBlank() || puerto == null) {
                    resultadosRed.add(
                        ResultadoEnvio("—", direccionCompleta, false, "IP o puerto inválido")
                    )
                    continue
                }

                val (okUdp, detalleUdp) = enviarUDP(ip, puerto, mensaje)
                resultadosRed.add(ResultadoEnvio("UDP", "$ip:$puerto", okUdp, detalleUdp))
            }
            estadoRed = "Envío por red terminado — revisa el detalle abajo"
        }
    }

    // Manda el mensaje como un único datagrama UDP. No hay conexión previa:
    // se dispara el paquete y ya (por eso es "fire and forget").
    private suspend fun enviarUDP(ip: String, puerto: Int, mensaje: String): Pair<Boolean, String> =
        withContext(Dispatchers.IO) {
            var socket: DatagramSocket? = null
            try {
                // Se crea un socket NUEVO en cada llamada — nunca se reutiliza
                // ni se comparte entre dispositivos ni entre envíos.
                socket = DatagramSocket()
                socket.soTimeout = 4000
                val datos = mensaje.toByteArray(Charsets.UTF_8)
                val direccion = InetAddress.getByName(ip)
                val paquete = DatagramPacket(datos, datos.size, direccion, puerto)
                socket.send(paquete)
                true to "Datagrama enviado correctamente"
            } catch (e: Exception) {
                false to (e.message ?: "Error UDP desconocido")
            } finally {
                // "finally" se ejecuta SIEMPRE, haya éxito o error — así el
                // socket queda cerrado y liberado de inmediato en todos los
                // casos, sin depender de nada más.
                socket?.close()
            }
        }

    private fun iniciarVerificacionPorProveedor(
        numero: String,
        textoEnviado: String,
        horaDeEnvio: Long,
        receiver: BroadcastReceiver
    ) {
        cancelarVerificacion()

        verificacionRunnable = object : Runnable {
            override fun run() {
                if (resultadoYaResuelto) return

                val encontrado = try {
                    existeEnBandejaDeEnviados(numero, textoEnviado, horaDeEnvio)
                } catch (e: Exception) {
                    false
                }

                if (encontrado) {
                    resultadoYaResuelto = true
                    cancelarTimeout()
                    try {
                        unregisterReceiver(receiver)
                    } catch (e: IllegalArgumentException) {
                    }
                    estado = EstadoApp.Exito("¡Ubicación enviada con éxito!")
                } else {
                    handlerVerificacion.postDelayed(this, 2000)
                }
            }
        }
        handlerVerificacion.postDelayed(verificacionRunnable!!, 2000)
    }

    // compara con PhoneNumberUtils porque el número guardado puede venir con otro formato (+57, espacios, etc)
    private fun existeEnBandejaDeEnviados(numero: String, texto: String, horaDeEnvio: Long): Boolean {
        val uri = Telephony.Sms.Sent.CONTENT_URI
        val projection = arrayOf(Telephony.Sms.ADDRESS, Telephony.Sms.BODY, Telephony.Sms.DATE)
        val selection = "${Telephony.Sms.DATE} >= ?"
        val selectionArgs = arrayOf((horaDeEnvio - 5000).toString())

        contentResolver.query(uri, projection, selection, selectionArgs, "${Telephony.Sms.DATE} DESC")
            ?.use { cursor ->
                val idxAddress = cursor.getColumnIndex(Telephony.Sms.ADDRESS)
                val idxBody = cursor.getColumnIndex(Telephony.Sms.BODY)
                while (cursor.moveToNext()) {
                    val direccionGuardada = cursor.getString(idxAddress) ?: continue
                    val cuerpoGuardado = cursor.getString(idxBody) ?: continue
                    val mismoNumero = PhoneNumberUtils.compare(direccionGuardada, numero)
                    val mismoTexto = cuerpoGuardado.trim() == texto.trim()
                    if (mismoNumero && mismoTexto) {
                        return true
                    }
                }
            }
        return false
    }

    private fun cancelarTimeout() {
        timeoutRunnable?.let { handlerTimeout.removeCallbacks(it) }
        timeoutRunnable = null
    }

    private fun cancelarVerificacion() {
        verificacionRunnable?.let { handlerVerificacion.removeCallbacks(it) }
        verificacionRunnable = null
    }

    override fun onDestroy() {
        super.onDestroy()
        cancelarTimeout()
        cancelarVerificacion()
        detenerTransmisionContinua()
    }
}

@Composable
fun PantallaPrincipal(
    estado: EstadoApp,
    onEnviarClick: (String) -> Unit,
    estadoRed: String,
    resultadosRed: List<ResultadoEnvio>,
    transmisionActiva: Boolean,
    onEnviarRedClick: (String) -> Unit
) {
    var numero by remember { mutableStateOf("") }
    var direccionIP by remember { mutableStateOf("") }

    MaterialTheme(
        colorScheme = lightColorScheme(
            primary = AzulPrincipal,
            onPrimary = Color.White,
            background = FondoClaro
        )
    ) {
        Surface(
            modifier = Modifier.fillMaxSize(),
            color = FondoClaro
        ) {
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .imePadding()
                    .verticalScroll(rememberScrollState())
                    .padding(24.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center
            ) {
                Icon(
                    imageVector = Icons.Default.LocationOn,
                    contentDescription = null,
                    tint = AzulPrincipal,
                    modifier = Modifier.size(72.dp)
                )

                Spacer(modifier = Modifier.height(12.dp))

                Text(
                    text = "Localizador SMS",
                    fontSize = 26.sp,
                    fontWeight = FontWeight.Bold,
                    color = AzulOscuro
                )

                Text(
                    text = "Envía tu ubicación GPS por mensaje de texto",
                    fontSize = 14.sp,
                    color = GrisTexto,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.padding(top = 4.dp, bottom = 32.dp)
                )

                OutlinedTextField(
                    value = numero,
                    onValueChange = { numero = it },
                    label = { Text("Número de teléfono destino") },
                    singleLine = true,
                    shape = RoundedCornerShape(16.dp),
                    modifier = Modifier.fillMaxWidth()
                )

                Spacer(modifier = Modifier.height(20.dp))

                TarjetaDeEstado(estado)

                Spacer(modifier = Modifier.height(24.dp))

                Button(
                    onClick = { onEnviarClick(numero) },
                    enabled = estado !is EstadoApp.Cargando,
                    shape = RoundedCornerShape(20.dp),
                    colors = ButtonDefaults.buttonColors(containerColor = AzulPrincipal),
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(56.dp)
                ) {
                    Icon(Icons.Default.Send, contentDescription = null)
                    Spacer(modifier = Modifier.width(8.dp))
                    Text("Obtener y enviar ubicación", fontSize = 16.sp)
                }

                // ============================================================
                //  FASE 2 (P1-S2): ENVÍO POR RED (UDP)
                // ============================================================
                Spacer(modifier = Modifier.height(32.dp))
                HorizontalDivider(color = GrisTexto.copy(alpha = 0.3f))
                Spacer(modifier = Modifier.height(20.dp))

                Text(
                    text = "Fase 2 — Envío por Red (UDP)",
                    fontSize = 16.sp,
                    fontWeight = FontWeight.Bold,
                    color = AzulOscuro
                )
                Text(
                    text = "El puerto (5000) ya está fijo — solo escribe la IP",
                    fontSize = 12.sp,
                    color = GrisTexto,
                    modifier = Modifier.padding(bottom = 12.dp, top = 2.dp)
                )

                OutlinedTextField(
                    value = direccionIP,
                    onValueChange = { direccionIP = it },
                    label = { Text("Dirección IP") },
                    placeholder = { Text("192.168.1.50") },
                    singleLine = true,
                    shape = RoundedCornerShape(16.dp),
                    modifier = Modifier.fillMaxWidth()
                )

                Spacer(modifier = Modifier.height(16.dp))

                // Tarjeta de estado de la parte de red (independiente de la del SMS)
                Card(
                    shape = RoundedCornerShape(16.dp),
                    colors = CardDefaults.cardColors(containerColor = FondoCargando.copy(alpha = 0.5f)),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(
                        text = estadoRed,
                        fontSize = 13.sp,
                        modifier = Modifier.padding(12.dp)
                    )
                }

                Spacer(modifier = Modifier.height(16.dp))

                Button(
                    onClick = { onEnviarRedClick(direccionIP) },
                    shape = RoundedCornerShape(20.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = if (transmisionActiva) Color(0xFFD32F2F) else AzulOscuro
                    ),
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(56.dp)
                ) {
                    Text(
                        if (transmisionActiva)
                            "⏸ Fin del recorrido"
                        else
                            "📡 Iniciar recorrido",
                        fontSize = 15.sp
                    )
                }

                // Bitácora: muestra cada intento (protocolo + destino + si funcionó)
                if (resultadosRed.isNotEmpty()) {
                    Spacer(modifier = Modifier.height(16.dp))
                    Column(modifier = Modifier.fillMaxWidth()) {
                        resultadosRed.forEach { resultado ->
                            val colorFondo = if (resultado.exito) FondoExito else FondoError
                            Card(
                                shape = RoundedCornerShape(12.dp),
                                colors = CardDefaults.cardColors(containerColor = colorFondo),
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .padding(bottom = 8.dp)
                            ) {
                                Column(modifier = Modifier.padding(12.dp)) {
                                    Text(
                                        text = "${if (resultado.exito) "✅" else "❌"} " +
                                                "${resultado.protocolo} → ${resultado.destino}",
                                        fontSize = 13.sp,
                                        fontWeight = FontWeight.Bold
                                    )
                                    Text(
                                        text = resultado.detalle,
                                        fontSize = 12.sp,
                                        color = GrisTexto
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun TarjetaDeEstado(estado: EstadoApp) {
    val (colorFondo, texto) = when (estado) {
        is EstadoApp.Esperando -> FondoClaro to "Escribe un número y presiona el botón para empezar"
        is EstadoApp.Cargando -> FondoCargando to estado.mensaje
        is EstadoApp.Exito -> FondoExito to estado.mensaje
        is EstadoApp.Error -> FondoError to estado.mensaje
        is EstadoApp.Incierto -> FondoIncierto to estado.mensaje
    }

    Card(
        shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(containerColor = colorFondo),
        modifier = Modifier.fillMaxWidth()
    ) {
        Row(
            modifier = Modifier.padding(16.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            if (estado is EstadoApp.Cargando) {
                CircularProgressIndicator(
                    modifier = Modifier.size(20.dp),
                    strokeWidth = 2.dp,
                    color = AzulPrincipal
                )
                Spacer(modifier = Modifier.width(12.dp))
            }
            Text(text = texto, fontSize = 14.sp)
        }
    }
}
