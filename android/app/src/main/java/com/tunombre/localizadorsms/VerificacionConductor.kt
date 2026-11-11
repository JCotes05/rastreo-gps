package com.tunombre.localizadorsms

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothSocket
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.ContextCompat
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.BufferedReader
import java.io.IOException
import java.io.InputStreamReader
import java.util.Locale
import java.util.UUID
import java.util.concurrent.atomic.AtomicReference

// ====================================================================
//  P3-S1 · VERIFICACIÓN DEL CONDUCTOR POR BLUETOOTH
//  El celular (que hace de módem satelital del camión) se conecta al
//  computador de reconocimiento facial, le pide IDENTIFICAR y espera:
//      IDENTIFICANDO                                   (ya está mirando)
//      CONDUCTOR;CED:1001000001;NOM:Jesús Cotes;SIM:0.82
//      NO_RECONOCIDO;SIM:0.21
//      ERROR;MOTIVO:...
//  Los campos se leen por su NOMBRE, no por posición.
// ====================================================================

/** Resultado de pedirle al computador que identifique al conductor. */
sealed class ResultadoVerificacion {
    data class Reconocido(val cedula: String, val nombre: String, val similitud: Float) : ResultadoVerificacion()
    data class NoReconocido(val similitud: Float) : ResultadoVerificacion()
    data class Fallo(val mensaje: String) : ResultadoVerificacion()
}

/** Un equipo Bluetooth vinculado al celular (para elegir el computador). */
data class EquipoBluetooth(val nombre: String, val direccion: String)

object VerificacionConductor {

    // UUID estándar del "puerto serie" Bluetooth (SPP). Es el que publica el puerto COM
    // entrante de Windows, y el mismo que usan el ESP32 de sensores y el ESP32-CAM (P3-S3).
    internal val UUID_SPP: UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")

    // Conexión + identificación. El computador responde en máximo ~10 s; esto es el colchón.
    private const val TIEMPO_MAXIMO_MS = 25_000L

    private const val PERMISO_CONECTAR = "android.permission.BLUETOOTH_CONNECT"   // Android 12+
    private const val PREFS = "rastreo_prefs"
    private const val PREF_PC_DIRECCION = "pc_direccion"
    private const val PREF_PC_NOMBRE = "pc_nombre"
    private val PATRON_CEDULA = Regex("^[0-9]{5,15}$")     // igual que en el servidor

    // La conexión en curso, para poder cortarla si se cancela o se agota el tiempo.
    private val socketActual = AtomicReference<BluetoothSocket?>(null)
    @Volatile private var tiempoAgotado = false

    // ---------------------------------------------------------------
    //  Bluetooth del celular y permisos
    // ---------------------------------------------------------------
    fun adaptador(contexto: Context): BluetoothAdapter? =
        (contexto.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter

    /** En Android 12+ hace falta el permiso "Dispositivos cercanos"; antes no se pide. */
    fun tienePermiso(contexto: Context): Boolean =
        Build.VERSION.SDK_INT < 31 ||
                ContextCompat.checkSelfPermission(contexto, PERMISO_CONECTAR) == PackageManager.PERMISSION_GRANTED

    fun permisoNecesario(): String? = if (Build.VERSION.SDK_INT >= 31) PERMISO_CONECTAR else null

    /** Equipos ya vinculados en los ajustes de Bluetooth del celular (no se buscan equipos nuevos). */
    @SuppressLint("MissingPermission")
    fun equiposVinculados(contexto: Context): List<EquipoBluetooth> {
        if (!tienePermiso(contexto)) return emptyList()
        val adaptador = adaptador(contexto) ?: return emptyList()
        return try {
            adaptador.bondedDevices.orEmpty()
                .map { EquipoBluetooth(it.name ?: it.address, it.address) }
                .sortedBy { it.nombre.lowercase(Locale.ROOT) }
        } catch (e: SecurityException) {
            emptyList()
        }
    }

    // ---------------------------------------------------------------
    //  Computador elegido (se guarda para no preguntarlo cada vez)
    // ---------------------------------------------------------------
    fun pcGuardado(contexto: Context): EquipoBluetooth? {
        val prefs = contexto.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val direccion = prefs.getString(PREF_PC_DIRECCION, null) ?: return null
        return EquipoBluetooth(prefs.getString(PREF_PC_NOMBRE, null) ?: direccion, direccion)
    }

    fun guardarPc(contexto: Context, equipo: EquipoBluetooth) {
        contexto.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString(PREF_PC_DIRECCION, equipo.direccion)
            .putString(PREF_PC_NOMBRE, equipo.nombre)
            .apply()
    }

    // ---------------------------------------------------------------
    //  Identificar al conductor
    // ---------------------------------------------------------------
    /** Corta la conexión en curso (si la hay). La pantalla lo usa para cancelar. */
    fun cancelar() {
        cerrar(socketActual.getAndSet(null))
    }

    /**
     * Se conecta al computador, le pide IDENTIFICAR y espera la respuesta.
     * onAvance recibe textos para la pantalla ("Conectando…", "Mira a la cámara…"),
     * siempre en el hilo principal. Todo lo lento corre en segundo plano.
     */
    @SuppressLint("MissingPermission")
    suspend fun identificar(
        contexto: Context,
        direccion: String,
        onAvance: (String) -> Unit
    ): ResultadoVerificacion {
        if (!tienePermiso(contexto)) return ResultadoVerificacion.Fallo("Falta el permiso de Bluetooth.")
        val adaptador = adaptador(contexto) ?: return ResultadoVerificacion.Fallo("Este celular no tiene Bluetooth.")
        if (!adaptador.isEnabled) return ResultadoVerificacion.Fallo("El Bluetooth está apagado.")
        val equipo = try {
            adaptador.getRemoteDevice(direccion)
        } catch (e: IllegalArgumentException) {
            return ResultadoVerificacion.Fallo("La dirección guardada del equipo de reconocimiento no es válida. Elígelo de nuevo.")
        }

        tiempoAgotado = false
        return coroutineScope {
            // Vigilante: si se pasa el tiempo, cierra la conexión y eso destraba la espera.
            val vigilante = launch {
                delay(TIEMPO_MAXIMO_MS)
                tiempoAgotado = true
                cancelar()
            }
            try {
                withContext(Dispatchers.IO) { conversar(equipo, onAvance) }
            } catch (e: IOException) {
                if (tiempoAgotado) {
                    ResultadoVerificacion.Fallo("El equipo de reconocimiento no respondió a tiempo.")
                } else {
                    ResultadoVerificacion.Fallo(
                        "No se pudo conectar con el equipo de reconocimiento (computador o ESP32-CAM). Revisa que esté encendido, cerca, " +
                                "con el Bluetooth activo y con el servicio de reconocimiento corriendo."
                    )
                }
            } catch (e: SecurityException) {
                ResultadoVerificacion.Fallo("Falta el permiso de Bluetooth.")
            } finally {
                vigilante.cancel()
                cancelar()
            }
        }
    }

    // Conversación completa con el computador (corre en segundo plano).
    private suspend fun conversar(equipo: BluetoothDevice, onAvance: (String) -> Unit): ResultadoVerificacion {
        val socket = conectar(equipo)
        val salida = socket.outputStream
        val entrada = BufferedReader(InputStreamReader(socket.inputStream, Charsets.UTF_8))

        salida.write("IDENTIFICAR\n".toByteArray(Charsets.UTF_8))
        salida.flush()
        avisar(onAvance, "Esperando al equipo de reconocimiento…")

        var linea = entrada.readLine()
        while (linea != null) {
            val (tipo, campos) = interpretar(linea)
            when (tipo) {
                "IDENTIFICANDO" -> avisar(onAvance, "Mira de frente a la cámara del equipo de reconocimiento…")
                "CONDUCTOR" -> {
                    val cedula = campos["CED"].orEmpty()
                    if (!PATRON_CEDULA.matches(cedula)) {
                        return ResultadoVerificacion.Fallo("El equipo de reconocimiento mandó una cédula inválida.")
                    }
                    val nombre = campos["NOM"]?.takeIf { it.isNotBlank() } ?: cedula
                    return ResultadoVerificacion.Reconocido(cedula, nombre, campos["SIM"]?.toFloatOrNull() ?: 0f)
                }
                "NO_RECONOCIDO" -> return ResultadoVerificacion.NoReconocido(campos["SIM"]?.toFloatOrNull() ?: 0f)
                "ERROR" -> return ResultadoVerificacion.Fallo(
                    "El equipo de reconocimiento respondió: ${campos["MOTIVO"] ?: "error desconocido"}."
                )
                // PONG u otros mensajes: se ignoran y se sigue esperando.
            }
            linea = entrada.readLine()
        }
        return ResultadoVerificacion.Fallo("El equipo de reconocimiento cerró la conexión sin responder.")
    }

    @SuppressLint("MissingPermission")
    private fun conectar(equipo: BluetoothDevice): BluetoothSocket {
        val normal = equipo.createRfcommSocketToServiceRecord(UUID_SPP)
        socketActual.set(normal)
        try {
            normal.connect()
            return normal
        } catch (e: IOException) {
            cerrar(normal)
            // Si fue porque se canceló o se agotó el tiempo, no se reintenta.
            if (tiempoAgotado || socketActual.get() == null) throw e
        }
        // Segundo intento sin cifrado obligatorio: algunos computadores con Windows lo necesitan.
        val insegura = equipo.createInsecureRfcommSocketToServiceRecord(UUID_SPP)
        socketActual.set(insegura)
        insegura.connect()
        return insegura
    }

    // "CONDUCTOR;CED:1001;NOM:Jesús" -> ("CONDUCTOR", {CED=1001, NOM=Jesús})
    private fun interpretar(linea: String): Pair<String, Map<String, String>> {
        val trozos = linea.trim().split(";")
        val tipo = trozos.first().trim().uppercase(Locale.ROOT)
        val campos = HashMap<String, String>()
        for (trozo in trozos.drop(1)) {
            val dosPuntos = trozo.indexOf(':')
            if (dosPuntos > 0) {
                campos[trozo.substring(0, dosPuntos).trim().uppercase(Locale.ROOT)] = trozo.substring(dosPuntos + 1).trim()
            }
        }
        return tipo to campos
    }

    private suspend fun avisar(onAvance: (String) -> Unit, texto: String) {
        withContext(Dispatchers.Main) { onAvance(texto) }
    }

    private fun cerrar(socket: BluetoothSocket?) {
        try {
            socket?.close()
        } catch (e: IOException) {
            // ya estaba cerrado
        }
    }
}