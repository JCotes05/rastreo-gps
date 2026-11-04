package com.tunombre.localizadorsms

import android.app.DatePickerDialog
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
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.LocalShipping
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Phone
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.mapSaver
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.snapshots.SnapshotStateMap
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.json.JSONObject
import java.util.Calendar
import java.util.Locale
import kotlin.math.abs

// ====================================================================
//  P3-S2 · PANTALLAS DEL CAMIÓN
//    PantallaAcceso   ingresar con placa y contraseña, o registrar el camión
//    PantallaCamion   el camión con sesión abierta:
//       - sin viaje      tarjeta del camión + "Disponible para viajes"
//       - viaje asignado mapa con la ruta, datos del envío y "Iniciar viaje"
//       - en viaje       mapa en vivo, avance, retraso, puntos de control y "Finalizar viaje"
//  Estas pantallas solo dibujan; lo que hacen los botones lo decide MainActivity.
// ====================================================================

internal val VerdeOk = Color(0xFF2BC48A)
internal val RojoAlerta = Color(0xFFFF5A4F)

// ---------------------------------------------------------------
//  Textos
// ---------------------------------------------------------------
internal fun textoKm(metros: Number): String =
    String.format(Locale.US, "%.1f", metros.toDouble() / 1000.0).replace('.', ',') + " km"

internal fun textoMiles(n: Int): String = String.format(Locale.US, "%,d", n).replace(',', '.')

internal fun textoDuracion(segundos: Int): String {
    val m = (segundos + 30) / 60
    return if (m < 60) "$m min" else "${m / 60} h ${String.format(Locale.US, "%02d", m % 60)} min"
}

internal fun textoRetraso(segundos: Int?): String {
    if (segundos == null) return "—"
    if (abs(segundos) < 60) return "A tiempo"
    val m = (abs(segundos) + 30) / 60
    val t = if (m < 60) "$m min" else "${m / 60} h ${m % 60} min"
    return t + if (segundos > 0) " tarde" else " antes"
}

// "2026-11-03T14:32:10-05:00" -> "14:32"
internal fun horaCorta(iso: String?): String = if (iso != null && iso.length >= 16) iso.substring(11, 16) else "—"

// "2026-11-03T14:32:10-05:00" -> "03/11 14:32"
internal fun fechaHoraCorta(iso: String?): String =
    if (iso != null && iso.length >= 16) "${iso.substring(8, 10)}/${iso.substring(5, 7)} ${iso.substring(11, 16)}" else "—"

// "2027-05-01" -> "01/05/2027"
internal fun fechaCorta(iso: String?): String =
    if (iso != null && iso.length >= 10) "${iso.substring(8, 10)}/${iso.substring(5, 7)}/${iso.substring(0, 4)}" else "—"

internal fun colorNivel(nivel: String?): Color = when (nivel) {
    "a_tiempo" -> VerdeOk
    "aviso" -> Ambar
    "alerta" -> RojoAlerta
    else -> GrisAzul
}

private fun colorEstadoViaje(estado: String): Color = when (estado) {
    "en_ruta" -> AzulMarca
    "en_destino" -> VerdeOk
    else -> GrisAzul
}

private fun nombreEstadoViaje(estado: String): String = when (estado) {
    "asignado" -> "Asignado"
    "en_ruta" -> "En ruta"
    "en_destino" -> "En destino"
    else -> estado
}

// ---------------------------------------------------------------
//  Piezas pequeñas con la identidad Orbita
// ---------------------------------------------------------------
@Composable
internal fun Etiqueta(texto: String, color: Color = GrisAzul, modifier: Modifier = Modifier) {
    Text(
        text = texto.uppercase(Locale.ROOT),
        fontFamily = Manrope,
        fontWeight = FontWeight.SemiBold,
        fontSize = 10.sp,
        letterSpacing = 1.sp,
        color = color,
        modifier = modifier
    )
}

@Composable
internal fun TextoFuerte(texto: String, tamano: TextUnit = 16.sp, color: Color = Claro, modifier: Modifier = Modifier) {
    Text(text = texto, fontFamily = Manrope, fontWeight = FontWeight.ExtraBold, fontSize = tamano, color = color, modifier = modifier)
}

@Composable
internal fun TextoSuave(
    texto: String,
    tamano: TextUnit = 13.sp,
    color: Color = GrisAzul,
    centrado: Boolean = false,
    modifier: Modifier = Modifier
) {
    Text(
        text = texto,
        fontFamily = Manrope,
        fontWeight = FontWeight.SemiBold,
        fontSize = tamano,
        color = color,
        textAlign = if (centrado) TextAlign.Center else TextAlign.Start,
        modifier = modifier
    )
}

@Composable
internal fun Insignia(texto: String, color: Color) {
    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(50))
            .background(color.copy(alpha = 0.16f))
            .border(1.dp, color.copy(alpha = 0.6f), RoundedCornerShape(50))
            .padding(horizontal = 10.dp, vertical = 3.dp)
    ) {
        Text(texto, fontFamily = Manrope, fontWeight = FontWeight.ExtraBold, fontSize = 11.sp, color = color, maxLines = 1)
    }
}

@Composable
internal fun TarjetaOrbita(
    borde: Color = GrisAzul,
    modifier: Modifier = Modifier,
    contenido: @Composable ColumnScope.() -> Unit
) {
    Card(
        shape = RoundedCornerShape(18.dp),
        colors = CardDefaults.cardColors(containerColor = Medio.copy(alpha = 0.55f)),
        border = BorderStroke(1.dp, borde.copy(alpha = 0.45f)),
        modifier = modifier.fillMaxWidth()
    ) {
        Column(modifier = Modifier.padding(16.dp), content = contenido)
    }
}

@Composable
private fun BotonPrincipal(
    texto: String,
    cargando: Boolean = false,
    enabled: Boolean = true,
    color: Color = AzulMarca,
    modifier: Modifier = Modifier,
    onClick: () -> Unit
) {
    Button(
        onClick = onClick,
        enabled = enabled && !cargando,
        shape = RoundedCornerShape(14.dp),
        colors = ButtonDefaults.buttonColors(
            containerColor = color,
            contentColor = Color.White,
            disabledContainerColor = color.copy(alpha = 0.35f),
            disabledContentColor = Color.White.copy(alpha = 0.7f)
        ),
        modifier = modifier
            .fillMaxWidth()
            .height(52.dp)
    ) {
        if (cargando) {
            CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp, color = Color.White)
            Spacer(modifier = Modifier.width(10.dp))
        }
        Text(texto, fontFamily = Manrope, fontWeight = FontWeight.ExtraBold, fontSize = 15.sp)
    }
}

@Composable
private fun BotonSecundario(texto: String, color: Color = Claro, modifier: Modifier = Modifier, onClick: () -> Unit) {
    OutlinedButton(
        onClick = onClick,
        shape = RoundedCornerShape(14.dp),
        border = BorderStroke(1.dp, color.copy(alpha = 0.6f)),
        colors = ButtonDefaults.outlinedButtonColors(contentColor = color),
        modifier = modifier
            .fillMaxWidth()
            .height(48.dp)
    ) {
        Text(texto, fontFamily = Manrope, fontWeight = FontWeight.ExtraBold, fontSize = 14.sp, textAlign = TextAlign.Center)
    }
}

/** Aviso que se puede cerrar (error en naranja, información en cian). */
@Composable
internal fun TarjetaAviso(texto: String, esError: Boolean, onCerrar: () -> Unit) {
    val color = if (esError) NaranjaMarca else CianMarca
    TarjetaOrbita(borde = color) {
        Row(verticalAlignment = Alignment.Top) {
            TextoSuave(texto, tamano = 14.sp, color = Claro, modifier = Modifier.weight(1f))
            Spacer(modifier = Modifier.width(8.dp))
            Text(
                "Cerrar",
                fontFamily = Manrope,
                fontWeight = FontWeight.ExtraBold,
                fontSize = 12.sp,
                color = color,
                modifier = Modifier
                    .clip(RoundedCornerShape(8.dp))
                    .clickable(onClick = onCerrar)
                    .padding(horizontal = 6.dp, vertical = 2.dp)
            )
        }
    }
}

@Composable
private fun coloresCampo() = OutlinedTextFieldDefaults.colors(
    focusedTextColor = Claro,
    unfocusedTextColor = Claro,
    disabledTextColor = Claro,
    focusedBorderColor = CianMarca,
    unfocusedBorderColor = GrisAzul.copy(alpha = 0.5f),
    disabledBorderColor = GrisAzul.copy(alpha = 0.5f),
    errorBorderColor = NaranjaMarca,
    focusedLabelColor = CianMarca,
    unfocusedLabelColor = GrisAzul,
    disabledLabelColor = GrisAzul,
    errorLabelColor = NaranjaMarca,
    cursorColor = CianMarca,
    focusedTrailingIconColor = GrisAzul,
    unfocusedTrailingIconColor = GrisAzul,
    disabledTrailingIconColor = GrisAzul,
    errorTrailingIconColor = NaranjaMarca,
    focusedSupportingTextColor = GrisAzul,
    unfocusedSupportingTextColor = GrisAzul,
    errorSupportingTextColor = NaranjaMarca
)

@Composable
private fun CampoTexto(
    valor: String,
    onCambio: (String) -> Unit,
    etiqueta: String,
    error: Boolean = false,
    ayuda: String? = null,
    teclado: KeyboardType = KeyboardType.Text,
    mayusculas: KeyboardCapitalization = KeyboardCapitalization.Sentences,
    esClave: Boolean = false
) {
    var verClave by remember { mutableStateOf(false) }
    OutlinedTextField(
        value = valor,
        onValueChange = onCambio,
        label = { Text(etiqueta, fontFamily = Manrope, fontWeight = FontWeight.SemiBold) },
        isError = error,
        singleLine = true,
        supportingText = ayuda?.let { { Text(it, fontFamily = Manrope, fontSize = 11.sp) } },
        keyboardOptions = KeyboardOptions(
            capitalization = mayusculas,
            keyboardType = if (esClave) KeyboardType.Password else teclado
        ),
        visualTransformation = if (esClave && !verClave) PasswordVisualTransformation() else VisualTransformation.None,
        trailingIcon = if (esClave) {
            {
                IconButton(onClick = { verClave = !verClave }) {
                    Icon(
                        imageVector = if (verClave) Icons.Filled.VisibilityOff else Icons.Filled.Visibility,
                        contentDescription = if (verClave) "Ocultar contraseña" else "Mostrar contraseña"
                    )
                }
            }
        } else null,
        textStyle = MaterialTheme.typography.bodyLarge.copy(fontFamily = Manrope, fontWeight = FontWeight.SemiBold),
        shape = RoundedCornerShape(12.dp),
        colors = coloresCampo(),
        modifier = Modifier.fillMaxWidth()
    )
}

/** Campo de solo lectura que al tocarlo abre algo (una lista o un calendario). */
@Composable
private fun CampoTocable(
    valor: String,
    etiqueta: String,
    error: Boolean,
    ayuda: String? = null,
    onTocar: () -> Unit,
    extra: @Composable () -> Unit = {}
) {
    Box {
        OutlinedTextField(
            value = valor,
            onValueChange = {},
            readOnly = true,
            label = { Text(etiqueta, fontFamily = Manrope, fontWeight = FontWeight.SemiBold) },
            isError = error,
            singleLine = true,
            supportingText = ayuda?.let { { Text(it, fontFamily = Manrope, fontSize = 11.sp) } },
            trailingIcon = { Icon(Icons.Filled.ArrowDropDown, contentDescription = null) },
            textStyle = MaterialTheme.typography.bodyLarge.copy(fontFamily = Manrope, fontWeight = FontWeight.SemiBold),
            shape = RoundedCornerShape(12.dp),
            colors = coloresCampo(),
            modifier = Modifier.fillMaxWidth()
        )
        // Capa transparente encima: el campo no abre el teclado, abre la lista o el calendario.
        Box(
            modifier = Modifier
                .matchParentSize()
                .padding(top = 8.dp)
                .clip(RoundedCornerShape(12.dp))
                .clickable(onClick = onTocar)
        )
        extra()
    }
}

@Composable
private fun CampoOpciones(
    valor: String,
    opciones: List<Pair<String, String>>,     // (código, nombre)
    etiqueta: String,
    error: Boolean,
    ayuda: String? = null,
    onCambio: (String) -> Unit
) {
    var abierto by remember { mutableStateOf(false) }
    val nombre = opciones.firstOrNull { it.first == valor }?.second ?: ""
    CampoTocable(nombre, etiqueta, error, ayuda, onTocar = { abierto = true }) {
        DropdownMenu(expanded = abierto, onDismissRequest = { abierto = false }) {
            opciones.forEach { (codigo, texto) ->
                DropdownMenuItem(
                    text = { Text(texto, fontFamily = Manrope, fontWeight = FontWeight.SemiBold) },
                    onClick = {
                        onCambio(codigo)
                        abierto = false
                    }
                )
            }
        }
    }
}

@Composable
private fun CampoFecha(valor: String, etiqueta: String, error: Boolean, onCambio: (String) -> Unit) {
    val contexto = LocalContext.current
    CampoTocable(if (valor.isEmpty()) "" else fechaCorta(valor), etiqueta, error, onTocar = {
        val c = Calendar.getInstance()
        if (valor.length == 10) {
            c.set(valor.substring(0, 4).toInt(), valor.substring(5, 7).toInt() - 1, valor.substring(8, 10).toInt())
        }
        DatePickerDialog(
            contexto,
            { _, anio, mes, dia -> onCambio(String.format(Locale.US, "%04d-%02d-%02d", anio, mes + 1, dia)) },
            c.get(Calendar.YEAR), c.get(Calendar.MONTH), c.get(Calendar.DAY_OF_MONTH)
        ).show()
    })
}

private fun limpiarPlaca(texto: String): String =
    texto.uppercase(Locale.ROOT).filter { it in 'A'..'Z' || it in '0'..'9' }.take(6)

// ====================================================================
//  ACCESO: INGRESAR O REGISTRAR EL CAMIÓN
// ====================================================================
private data class Configuracion(val codigo: String, val nombre: String, val pbv: Int)

// Las mismas de la API (Res. 4100 de 2004): la capacidad debe ser menor que el peso bruto máximo.
private val CONFIGURACIONES = listOf(
    Configuracion("C2", "C2 · Turbo o sencillo (2 ejes)", 16000),
    Configuracion("C3", "C3 · Doble troque (3 ejes)", 28000),
    Configuracion("C4", "C4 · Cuatro ejes", 36000),
    Configuracion("2S2", "2S2 · Tractocamión (4 ejes)", 32000),
    Configuracion("2S3", "2S3 · Tractocamión (5 ejes)", 40500),
    Configuracion("3S2", "3S2 · Tractomula (5 ejes)", 48000),
    Configuracion("3S3", "3S3 · Tractomula (6 ejes)", 52000)
)
private val CARROCERIAS = listOf(
    "furgon" to "Furgón", "refrigerado" to "Furgón refrigerado", "estacas" to "Estacas",
    "planchon" to "Planchón", "volco" to "Volco", "tanque" to "Tanque", "portacontenedor" to "Portacontenedor"
)

private val GuardarCampos = mapSaver<SnapshotStateMap<String, String>>(
    save = { it.toMap() },
    restore = { guardado ->
        mutableStateMapOf<String, String>().apply { guardado.forEach { (k, v) -> put(k, v as String) } }
    }
)

/** Revisa el formulario como lo revisará el servidor. Devuelve (campo, mensaje) del primer error. */
private fun revisarRegistro(c: Map<String, String>): Pair<String, String>? {
    fun v(campo: String) = c[campo].orEmpty().trim()
    if (!Regex("^[A-Z]{3}[0-9]{3}$").matches(v("placa")))
        return "placa" to "La placa debe tener 3 letras y 3 números (por ejemplo TKL482)."
    if (c["clave"].orEmpty().length < 8) return "clave" to "La contraseña debe tener al menos 8 caracteres."
    if (c["clave"] != c["confirmar"]) return "confirmar" to "Las dos contraseñas no coinciden."
    val conf = CONFIGURACIONES.firstOrNull { it.codigo == v("configuracion") }
        ?: return "configuracion" to "Elige la configuración del camión."
    if (CARROCERIAS.none { it.first == v("carroceria") }) return "carroceria" to "Elige la carrocería."
    val capacidad = v("capacidad_kg").toIntOrNull()
    if (capacidad == null || capacidad < 1) return "capacidad_kg" to "Escribe la capacidad de carga en kg."
    if (capacidad >= conf.pbv)
        return "capacidad_kg" to "La capacidad debe ser menor que ${textoMiles(conf.pbv)} kg, el peso bruto máximo de un ${conf.codigo}."
    if (v("marca").isEmpty()) return "marca" to "Falta la marca."
    if (v("linea").isEmpty()) return "linea" to "Falta la línea."
    val maximo = Calendar.getInstance().get(Calendar.YEAR) + 1
    val modelo = v("modelo").toIntOrNull()
    if (modelo == null || modelo < 1970 || modelo > maximo) return "modelo" to "El modelo debe ser un año entre 1970 y $maximo."
    if (v("color").isEmpty()) return "color" to "Falta el color."
    if (v("propietario_nombre").isEmpty()) return "propietario_nombre" to "Falta el nombre del dueño."
    if (!Regex("^[0-9]{5,15}(-[0-9])?$").matches(v("propietario_documento")))
        return "propietario_documento" to "El documento del dueño debe tener solo números (cédula o NIT)."
    if (!Regex("^3[0-9]{9}$").matches(v("propietario_celular")))
        return "propietario_celular" to "El celular debe tener 10 dígitos y empezar por 3."
    if (v("soat_vence").isEmpty()) return "soat_vence" to "Elige la fecha de vencimiento del SOAT."
    if (v("tecnomecanica_vence").isEmpty()) return "tecnomecanica_vence" to "Elige la fecha de vencimiento de la técnico-mecánica."
    return null
}

private fun registroComoJson(c: Map<String, String>): JSONObject {
    fun v(campo: String) = c[campo].orEmpty().trim()
    return JSONObject()
        .put("placa", v("placa"))
        .put("clave", c["clave"].orEmpty())
        .put("configuracion", v("configuracion"))
        .put("carroceria", v("carroceria"))
        .put("capacidad_kg", v("capacidad_kg").toInt())
        .put("marca", v("marca"))
        .put("linea", v("linea"))
        .put("modelo", v("modelo").toInt())
        .put("color", v("color"))
        .put("propietario_nombre", v("propietario_nombre"))
        .put("propietario_documento", v("propietario_documento"))
        .put("propietario_celular", v("propietario_celular"))
        .put("soat_vence", v("soat_vence"))
        .put("tecnomecanica_vence", v("tecnomecanica_vence"))
}

@Composable
fun PantallaAcceso(
    cargando: Boolean,
    error: String?,
    campoError: String?,
    onIngresar: (placa: String, clave: String) -> Unit,
    onRegistrar: (JSONObject) -> Unit,
    onLimpiarError: () -> Unit
) {
    var registrando by rememberSaveable { mutableStateOf(false) }
    val desplazamiento = rememberScrollState()

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Brush.verticalGradient(listOf(Oscuro, FondoAbajo)))
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .systemBarsPadding()
                .imePadding()
                .verticalScroll(desplazamiento)
                .padding(horizontal = 24.dp, vertical = 16.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Spacer(modifier = Modifier.height(12.dp))
            Image(
                painter = painterResource(R.drawable.orbita_logo),
                contentDescription = "Orbita. Cada kilómetro bajo tu radar",
                modifier = Modifier
                    .width(220.dp)
                    .aspectRatio(714f / 254f)
            )
            Spacer(modifier = Modifier.height(20.dp))

            // Selector Ingresar / Registrar
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(14.dp))
                    .background(Medio.copy(alpha = 0.55f))
                    .padding(4.dp)
            ) {
                listOf(false to "Ingresar", true to "Registrar camión").forEach { (esRegistro, texto) ->
                    val elegido = registrando == esRegistro
                    Box(
                        modifier = Modifier
                            .weight(1f)
                            .clip(RoundedCornerShape(11.dp))
                            .background(if (elegido) AzulMarca else Color.Transparent)
                            .clickable {
                                registrando = esRegistro
                                onLimpiarError()
                            }
                            .padding(vertical = 11.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            texto,
                            fontFamily = Manrope,
                            fontWeight = FontWeight.ExtraBold,
                            fontSize = 14.sp,
                            color = if (elegido) Color.White else GrisAzul
                        )
                    }
                }
            }
            Spacer(modifier = Modifier.height(20.dp))

            if (registrando) {
                FormularioRegistro(cargando, error, campoError, onRegistrar, onLimpiarError)
            } else {
                FormularioIngreso(cargando, error, campoError, onIngresar, onLimpiarError)
            }
            Spacer(modifier = Modifier.height(24.dp))
        }
    }
}

@Composable
private fun FormularioIngreso(
    cargando: Boolean,
    error: String?,
    campoError: String?,
    onIngresar: (String, String) -> Unit,
    onLimpiarError: () -> Unit
) {
    var placa by rememberSaveable { mutableStateOf("") }
    var clave by rememberSaveable { mutableStateOf("") }

    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        TextoSuave(
            "Ingresa con la placa del camión y la contraseña que creaste al registrarlo.",
            tamano = 14.sp
        )
        CampoTexto(
            placa, { placa = limpiarPlaca(it); onLimpiarError() }, "Placa (por ejemplo TKL482)",
            error = campoError == "placa", teclado = KeyboardType.Ascii, mayusculas = KeyboardCapitalization.Characters
        )
        CampoTexto(clave, { clave = it; onLimpiarError() }, "Contraseña", error = campoError == "clave", esClave = true)
        if (error != null) TextoSuave(error, tamano = 14.sp, color = NaranjaMarca)
        Spacer(modifier = Modifier.height(4.dp))
        BotonPrincipal("Ingresar", cargando = cargando, enabled = placa.length == 6 && clave.isNotEmpty()) {
            onIngresar(placa, clave)
        }
    }
}

@Composable
private fun FormularioRegistro(
    cargando: Boolean,
    errorServidor: String?,
    campoServidor: String?,
    onRegistrar: (JSONObject) -> Unit,
    onLimpiarError: () -> Unit
) {
    val campos = rememberSaveable(saver = GuardarCampos) { mutableStateMapOf<String, String>() }
    var errorLocal by remember { mutableStateOf<Pair<String, String>?>(null) }
    val campoError = errorLocal?.first ?: campoServidor
    val mensajeError = errorLocal?.second ?: errorServidor

    fun valor(campo: String) = campos[campo].orEmpty()
    fun cambiar(campo: String, nuevo: String) {
        campos[campo] = nuevo
        errorLocal = null
        onLimpiarError()
    }

    val pbv = CONFIGURACIONES.firstOrNull { it.codigo == valor("configuracion") }?.pbv

    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        TextoSuave(
            "El dueño registra el camión una vez. Después, el conductor ingresa con la placa y la contraseña.",
            tamano = 14.sp
        )

        Etiqueta("Acceso", color = CianMarca, modifier = Modifier.padding(top = 8.dp))
        CampoTexto(
            valor("placa"), { cambiar("placa", limpiarPlaca(it)) }, "Placa",
            error = campoError == "placa", teclado = KeyboardType.Ascii, mayusculas = KeyboardCapitalization.Characters
        )
        CampoTexto(
            valor("clave"), { cambiar("clave", it) }, "Contraseña (mínimo 8 caracteres)",
            error = campoError == "clave", esClave = true
        )
        CampoTexto(
            valor("confirmar"), { cambiar("confirmar", it) }, "Repite la contraseña",
            error = campoError == "confirmar", esClave = true
        )

        Etiqueta("Vehículo", color = CianMarca, modifier = Modifier.padding(top = 8.dp))
        CampoOpciones(
            valor("configuracion"), CONFIGURACIONES.map { it.codigo to it.nombre }, "Configuración (ejes)",
            error = campoError == "configuracion"
        ) { cambiar("configuracion", it) }
        CampoOpciones(valor("carroceria"), CARROCERIAS, "Carrocería", error = campoError == "carroceria") {
            cambiar("carroceria", it)
        }
        CampoTexto(
            valor("capacidad_kg"), { cambiar("capacidad_kg", it.filter(Char::isDigit).take(5)) }, "Capacidad de carga (kg)",
            error = campoError == "capacidad_kg", teclado = KeyboardType.Number,
            ayuda = pbv?.let { "Menor que ${textoMiles(it)} kg (peso bruto máximo)" }
        )
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Box(modifier = Modifier.weight(1f)) {
                CampoTexto(valor("marca"), { cambiar("marca", it.take(40)) }, "Marca", error = campoError == "marca")
            }
            Box(modifier = Modifier.weight(1f)) {
                CampoTexto(valor("linea"), { cambiar("linea", it.take(40)) }, "Línea", error = campoError == "linea")
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Box(modifier = Modifier.weight(1f)) {
                CampoTexto(
                    valor("modelo"), { cambiar("modelo", it.filter(Char::isDigit).take(4)) }, "Modelo (año)",
                    error = campoError == "modelo", teclado = KeyboardType.Number
                )
            }
            Box(modifier = Modifier.weight(1f)) {
                CampoTexto(valor("color"), { cambiar("color", it.take(40)) }, "Color", error = campoError == "color")
            }
        }

        Etiqueta("Dueño", color = CianMarca, modifier = Modifier.padding(top = 8.dp))
        CampoTexto(
            valor("propietario_nombre"), { cambiar("propietario_nombre", it.take(80)) }, "Nombre del dueño",
            error = campoError == "propietario_nombre", mayusculas = KeyboardCapitalization.Words
        )
        CampoTexto(
            valor("propietario_documento"),
            { cambiar("propietario_documento", it.filter { ch -> ch.isDigit() || ch == '-' }.take(17)) },
            "Cédula o NIT", error = campoError == "propietario_documento", teclado = KeyboardType.Number
        )
        CampoTexto(
            valor("propietario_celular"), { cambiar("propietario_celular", it.filter(Char::isDigit).take(10)) }, "Celular",
            error = campoError == "propietario_celular", teclado = KeyboardType.Phone
        )

        Etiqueta("Documentos", color = CianMarca, modifier = Modifier.padding(top = 8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Box(modifier = Modifier.weight(1f)) {
                CampoFecha(valor("soat_vence"), "SOAT vence", campoError == "soat_vence") { cambiar("soat_vence", it) }
            }
            Box(modifier = Modifier.weight(1f)) {
                CampoFecha(valor("tecnomecanica_vence"), "Técnico-mec. vence", campoError == "tecnomecanica_vence") {
                    cambiar("tecnomecanica_vence", it)
                }
            }
        }

        if (mensajeError != null) TextoSuave(mensajeError, tamano = 14.sp, color = NaranjaMarca)
        Spacer(modifier = Modifier.height(4.dp))
        BotonPrincipal("Registrar camión", cargando = cargando) {
            val problema = revisarRegistro(campos)
            if (problema != null) errorLocal = problema else onRegistrar(registroComoJson(campos))
        }
    }
}

// ====================================================================
//  CAMIÓN CON SESIÓN ABIERTA
// ====================================================================

/** Lo que pueden hacer los botones de estas pantallas (lo implementa MainActivity). */
class AccionesCamion(
    val onDisponible: (Boolean) -> Unit,
    val onBotonViaje: () -> Unit,          // iniciar/reanudar (verifica primero) o cancelar la verificación
    val onSinVerificar: () -> Unit,
    val onFinalizar: () -> Unit,
    val onDetener: () -> Unit,
    val onTransmitirSinViaje: () -> Unit,
    val onActualizar: () -> Unit,
    val onCambiarPc: () -> Unit,
    val onCerrarSesion: () -> Unit,
    val onCerrarAviso: () -> Unit,
    val onLlamar: (String) -> Unit
)

@Composable
fun PantallaCamion(
    camion: Camion,
    viaje: Viaje?,
    rutaJson: String?,
    posicionTelefono: Pair<Double, Double>?,
    fase: FaseVerificacion,
    aviso: String?,
    avisoEsError: Boolean,
    sinConexion: Boolean,
    cambiandoDisponibilidad: Boolean,
    finalizando: Boolean,
    pcNombre: String?,
    acciones: AccionesCamion
) {
    val transmitiendoEste = viaje != null && EstadoTransmision.activa && EstadoTransmision.envioId == viaje.id
    val enCurso = viaje != null && (viaje.estado != "asignado" || transmitiendoEste)

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Brush.verticalGradient(listOf(Oscuro, FondoAbajo)))
    ) {
        Column(modifier = Modifier
            .fillMaxSize()
            .systemBarsPadding()) {
            BarraCamion(camion, sinConexion, acciones)
            when {
                viaje == null -> ContenidoEspera(camion, aviso, avisoEsError, cambiandoDisponibilidad, acciones)
                enCurso -> ContenidoEnViaje(
                    viaje, rutaJson, posicionTelefono, fase, aviso, avisoEsError, finalizando, pcNombre, acciones
                )
                else -> ContenidoViajeAsignado(viaje, rutaJson, posicionTelefono, fase, aviso, avisoEsError, pcNombre, acciones)
            }
        }
    }
}

@Composable
private fun BarraCamion(camion: Camion, sinConexion: Boolean, acciones: AccionesCamion) {
    var menu by remember { mutableStateOf(false) }
    Column {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(start = 16.dp, end = 4.dp, top = 6.dp, bottom = 6.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Image(
                painter = painterResource(R.drawable.orbita_logo),
                contentDescription = "Orbita",
                modifier = Modifier
                    .height(34.dp)
                    .aspectRatio(714f / 254f)
            )
            Spacer(modifier = Modifier.weight(1f))
            Insignia(camion.placa, CianMarca)
            Box {
                IconButton(onClick = { menu = true }) {
                    Icon(Icons.Filled.MoreVert, contentDescription = "Menú", tint = Claro)
                }
                DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                    DropdownMenuItem(
                        text = { Text("Actualizar", fontFamily = Manrope, fontWeight = FontWeight.SemiBold) },
                        onClick = { menu = false; acciones.onActualizar() }
                    )
                    DropdownMenuItem(
                        text = { Text("Computador de reconocimiento", fontFamily = Manrope, fontWeight = FontWeight.SemiBold) },
                        onClick = { menu = false; acciones.onCambiarPc() }
                    )
                    DropdownMenuItem(
                        text = { Text("Cerrar sesión", fontFamily = Manrope, fontWeight = FontWeight.SemiBold) },
                        onClick = { menu = false; acciones.onCerrarSesion() }
                    )
                }
            }
        }
        if (sinConexion) {
            Text(
                "Sin conexión con el servidor · reintentando…",
                fontFamily = Manrope,
                fontWeight = FontWeight.SemiBold,
                fontSize = 12.sp,
                color = Oscuro,
                textAlign = TextAlign.Center,
                modifier = Modifier
                    .fillMaxWidth()
                    .background(Ambar)
                    .padding(vertical = 4.dp)
            )
        }
    }
}

// ---------------------------------------------------------------
//  Sin viaje: el camión y su disponibilidad
// ---------------------------------------------------------------
@Composable
private fun ContenidoEspera(
    camion: Camion,
    aviso: String?,
    avisoEsError: Boolean,
    cambiando: Boolean,
    acciones: AccionesCamion
) {
    val disponible = camion.estado == "disponible"
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 20.dp, vertical = 8.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        if (aviso != null) TarjetaAviso(aviso, avisoEsError, acciones.onCerrarAviso)

        RadarEspera(activo = disponible)
        TextoFuerte(if (disponible) "Esperando un viaje" else "No disponible", tamano = 22.sp)
        TextoSuave(
            if (disponible) {
                "Cuando la empresa te asigne un envío aparecerá aquí. Deja la app abierta: así la empresa ve dónde estás."
            } else {
                "Activa \"Disponible\" cuando puedas recibir un viaje."
            },
            tamano = 14.sp,
            centrado = true
        )

        // Disponibilidad
        TarjetaOrbita(borde = if (disponible) CianMarca else GrisAzul) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(modifier = Modifier.weight(1f)) {
                    TextoFuerte("Disponible para viajes", tamano = 16.sp)
                    TextoSuave(
                        when {
                            !camion.documentosVigentes -> "Tienes documentos vencidos: actualízalos para poder recibir viajes."
                            disponible && camion.ubicacionHaceMin != null ->
                                "Ubicación enviada hace ${camion.ubicacionHaceMin} min"
                            disponible -> "La empresa te puede asignar un envío"
                            else -> "La empresa no te asignará envíos"
                        },
                        color = if (!camion.documentosVigentes) NaranjaMarca else GrisAzul,
                        modifier = Modifier.padding(top = 2.dp)
                    )
                }
                Spacer(modifier = Modifier.width(12.dp))
                if (cambiando) {
                    CircularProgressIndicator(modifier = Modifier.size(28.dp), strokeWidth = 3.dp, color = CianMarca)
                } else {
                    Switch(
                        checked = disponible,
                        onCheckedChange = { acciones.onDisponible(it) },
                        enabled = camion.documentosVigentes || disponible,
                        colors = SwitchDefaults.colors(
                            checkedThumbColor = Color.White,
                            checkedTrackColor = AzulMarca,
                            uncheckedThumbColor = GrisAzul,
                            uncheckedTrackColor = Oscuro,
                            uncheckedBorderColor = GrisAzul
                        )
                    )
                }
            }
        }

        TarjetaDelCamion(camion)

        Text(
            "Transmitir sin viaje (solo rastreo, como en S1)",
            fontFamily = Manrope,
            fontWeight = FontWeight.SemiBold,
            fontSize = 13.sp,
            color = CianMarca,
            modifier = Modifier
                .clip(RoundedCornerShape(8.dp))
                .clickable(onClick = acciones.onTransmitirSinViaje)
                .padding(horizontal = 8.dp, vertical = 8.dp)
        )
        PieServidor()
    }
}

@Composable
private fun RadarEspera(activo: Boolean) {
    Box(modifier = Modifier.size(190.dp), contentAlignment = Alignment.Center) {
        Canvas(modifier = Modifier.fillMaxSize()) {
            val radio = size.minDimension / 2f
            drawCircle(
                brush = Brush.radialGradient(
                    colors = listOf(AzulMarca.copy(alpha = if (activo) 0.28f else 0.12f), Color.Transparent),
                    radius = radio
                ),
                radius = radio
            )
            drawCircle(color = CianMarca.copy(alpha = 0.08f), radius = radio * 0.82f, style = Stroke(width = 1.dp.toPx()))
        }
        if (activo) PulsosRadar(color = CianMarca, modifier = Modifier.fillMaxSize())
        Box(
            modifier = Modifier
                .size(112.dp)
                .clip(CircleShape)
                .background(Brush.radialGradient(listOf(Medio, Oscuro)))
                .border(2.dp, if (activo) CianMarca else GrisAzul.copy(alpha = 0.5f), CircleShape),
            contentAlignment = Alignment.Center
        ) {
            Icon(
                Icons.Filled.LocalShipping,
                contentDescription = null,
                tint = if (activo) CianMarca else GrisAzul,
                modifier = Modifier.size(50.dp)
            )
        }
    }
}

@Composable
private fun TarjetaDelCamion(camion: Camion) {
    TarjetaOrbita {
        Etiqueta("Tu camión")
        TextoFuerte(camion.placa, tamano = 24.sp, modifier = Modifier.padding(top = 2.dp))
        TextoSuave(camion.configuracionNombre, tamano = 14.sp, color = Claro, modifier = Modifier.padding(top = 4.dp))
        TextoSuave("${camion.carroceriaNombre} · capacidad ${textoMiles(camion.capacidadKg)} kg", tamano = 14.sp)
        TextoSuave("${camion.marca} ${camion.linea} ${camion.modelo} · ${camion.color}", tamano = 14.sp)
        Spacer(modifier = Modifier.height(10.dp))
        Row {
            Dato("SOAT HASTA", fechaCorta(camion.soatVence), modifier = Modifier.weight(1f))
            Dato("TÉCNICO-MEC. HASTA", fechaCorta(camion.tecnomecanicaVence), modifier = Modifier.weight(1f))
        }
        if (camion.propietario != null) {
            TextoSuave("Dueño: ${camion.propietario}", modifier = Modifier.padding(top = 8.dp))
        }
    }
}

@Composable
private fun PieServidor() {
    TextoSuave(
        "API ${Config.URL_API.removePrefix("https://")} · ubicación por UDP a ${Config.IP_SERVIDOR}:${Config.PUERTO}",
        tamano = 11.sp,
        color = GrisAzul.copy(alpha = 0.65f),
        centrado = true
    )
}

// ---------------------------------------------------------------
//  Viaje asignado (todavía no ha salido)
// ---------------------------------------------------------------
@Composable
private fun ContenidoViajeAsignado(
    viaje: Viaje,
    rutaJson: String?,
    posicionTelefono: Pair<Double, Double>?,
    fase: FaseVerificacion,
    aviso: String?,
    avisoEsError: Boolean,
    pcNombre: String?,
    acciones: AccionesCamion
) {
    val viajeJson = remember(viaje) { viaje.json.toString() }
    MapaViaje(
        rutaJson = rutaJson,
        viajeJson = viajeJson,
        camion = posicionTelefono,
        seguirCamion = false,
        modifier = Modifier
            .fillMaxWidth()
            .height(240.dp)
    )
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 20.dp, vertical = 14.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        if (aviso != null) TarjetaAviso(aviso, avisoEsError, acciones.onCerrarAviso)

        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Etiqueta("Viaje asignado", color = CianMarca)
            Insignia(viaje.codigo, CianMarca)
        }
        TextoFuerte("${viaje.origenDireccion} → ${viaje.destinoDireccion}", tamano = 19.sp)
        TextoSuave(
            "${textoKm(viaje.distanciaM)} · ${textoDuracion(viaje.duracionS)} · ${viaje.puntosDeControl} puntos de control",
            tamano = 14.sp
        )

        ZonaDeInicio(
            fase = fase,
            titulo = "Iniciar viaje",
            subtitulo = "Toca el botón: primero se verifica al conductor con la cámara del computador",
            pcNombre = pcNombre,
            acciones = acciones
        )

        TarjetaParada(
            etiqueta = "Recoger en",
            direccion = viaje.origenDireccion,
            persona = viaje.origenContacto,
            celular = viaje.origenCelular,
            nota = viaje.cargueProgramado?.let { "Cargue programado: ${fechaHoraCorta(it)}" },
            onLlamar = acciones.onLlamar
        )
        TarjetaParada(
            etiqueta = "Entregar en",
            direccion = viaje.destinoDireccion,
            persona = viaje.destinatario,
            celular = viaje.destinoCelular,
            nota = null,
            onLlamar = acciones.onLlamar
        )
        TarjetaCarga(viaje)
        PieServidor()
    }
}

/** Botón radar + estado de la verificación (para iniciar o reanudar el viaje). */
@Composable
private fun ZonaDeInicio(
    fase: FaseVerificacion,
    titulo: String,
    subtitulo: String,
    pcNombre: String?,
    acciones: AccionesCamion
) {
    val verificando = fase is FaseVerificacion.EnCurso
    Column(modifier = Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
        BotonRadar(
            estado = if (verificando) EstadoBoton.VERIFICANDO else EstadoBoton.REPOSO,
            onClick = acciones.onBotonViaje,
            tamano = 200.dp
        )
        TextoFuerte(
            when (fase) {
                is FaseVerificacion.EnCurso -> "Verificando conductor"
                is FaseVerificacion.Fallida -> fase.titulo
                else -> titulo
            },
            tamano = 21.sp,
            color = if (fase is FaseVerificacion.Fallida) NaranjaMarca else Claro
        )
        TextoSuave(
            when (fase) {
                is FaseVerificacion.EnCurso -> fase.texto
                is FaseVerificacion.Fallida -> "Puedes reintentar o salir sin verificar"
                else -> subtitulo
            },
            tamano = 13.sp,
            centrado = true,
            modifier = Modifier.padding(top = 4.dp, bottom = 10.dp)
        )
        when (fase) {
            is FaseVerificacion.Fallida -> TarjetaVerificacionFallida(
                detalle = fase.detalle,
                onReintentar = acciones.onBotonViaje,
                onSinVerificar = acciones.onSinVerificar
            )
            is FaseVerificacion.EnCurso -> TextoSuave(
                "La cámara del computador debe ver tu cara de frente y con buena luz. Toca el botón para cancelar.",
                tamano = 12.sp,
                centrado = true
            )
            else -> Text(
                text = "Computador de reconocimiento: ${pcNombre ?: "sin elegir"}  ·  Cambiar",
                fontFamily = Manrope,
                fontWeight = FontWeight.SemiBold,
                fontSize = 12.sp,
                color = CianMarca,
                textAlign = TextAlign.Center,
                modifier = Modifier
                    .clip(RoundedCornerShape(8.dp))
                    .clickable(onClick = acciones.onCambiarPc)
                    .padding(horizontal = 8.dp, vertical = 6.dp)
            )
        }
    }
}

@Composable
private fun TarjetaParada(
    etiqueta: String,
    direccion: String,
    persona: String?,
    celular: String?,
    nota: String?,
    onLlamar: (String) -> Unit
) {
    TarjetaOrbita {
        Etiqueta(etiqueta)
        TextoFuerte(direccion, tamano = 16.sp, modifier = Modifier.padding(top = 2.dp))
        if (nota != null) TextoSuave(nota, modifier = Modifier.padding(top = 2.dp))
        if (persona != null || celular != null) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.padding(top = 8.dp)
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    if (persona != null) TextoSuave(persona, tamano = 14.sp, color = Claro)
                    if (celular != null) TextoSuave(celular, tamano = 13.sp)
                }
                if (celular != null) {
                    IconButton(
                        onClick = { onLlamar(celular) },
                        modifier = Modifier
                            .clip(CircleShape)
                            .background(AzulMarca.copy(alpha = 0.25f))
                    ) {
                        Icon(Icons.Filled.Phone, contentDescription = "Llamar a ${persona ?: celular}", tint = CianMarca)
                    }
                }
            }
        }
    }
}

@Composable
private fun TarjetaCarga(viaje: Viaje) {
    TarjetaOrbita {
        Etiqueta("Carga")
        TextoFuerte(viaje.descripcion.ifEmpty { viaje.cargaTipo }, tamano = 16.sp, modifier = Modifier.padding(top = 2.dp))
        TextoSuave(viaje.cargaTipo, tamano = 14.sp)
        Spacer(modifier = Modifier.height(10.dp))
        Row {
            Dato("PESO", "${textoMiles(viaje.pesoKg)} kg", modifier = Modifier.weight(1f))
            Dato(
                "EMPAQUE",
                (viaje.cantidad?.let { "$it " } ?: "") + viaje.empaque.lowercase(Locale.ROOT),
                modifier = Modifier.weight(1f)
            )
        }
        if (viaje.cliente.isNotEmpty()) TextoSuave("Cliente: ${viaje.cliente}", modifier = Modifier.padding(top = 8.dp))
    }
}

// ---------------------------------------------------------------
//  En viaje: seguimiento en vivo
// ---------------------------------------------------------------
@Composable
private fun ContenidoEnViaje(
    viaje: Viaje,
    rutaJson: String?,
    posicionTelefono: Pair<Double, Double>?,
    fase: FaseVerificacion,
    aviso: String?,
    avisoEsError: Boolean,
    finalizando: Boolean,
    pcNombre: String?,
    acciones: AccionesCamion
) {
    val transmitiendo = EstadoTransmision.activa && EstadoTransmision.envioId == viaje.id
    val viajeJson = remember(viaje) { viaje.json.toString() }
    var confirmarFinalizar by remember { mutableStateOf(false) }
    var confirmarDetener by remember { mutableStateOf(false) }
    val enDestino = viaje.estado == "en_destino"

    MapaViaje(
        rutaJson = rutaJson,
        viajeJson = viajeJson,
        camion = (if (transmitiendo) EstadoTransmision.posicion else null) ?: posicionTelefono,
        seguirCamion = true,
        modifier = Modifier
            .fillMaxWidth()
            .height(270.dp)
    )
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 20.dp, vertical = 14.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Insignia(viaje.codigo, CianMarca)
            Insignia(nombreEstadoViaje(viaje.estado), colorEstadoViaje(viaje.estado))
            if (viaje.estado == "en_ruta" && viaje.retrasoS != null) {
                Insignia(textoRetraso(viaje.retrasoS), colorNivel(viaje.nivelRetraso))
            }
        }
        TextoFuerte("${viaje.origenDireccion} → ${viaje.destinoDireccion}", tamano = 17.sp)

        if (aviso != null) TarjetaAviso(aviso, avisoEsError, acciones.onCerrarAviso)

        // Avisos del seguimiento
        val desvio = viaje.desvioActivo
        val retraso = viaje.retrasoActivo
        when {
            enDestino -> TarjetaEstado(
                VerdeOk, "Llegaste al destino",
                "Entrega la carga a ${viaje.destinatario ?: "quien la recibe"} y toca \"Finalizar viaje\"."
            )
            desvio != null -> TarjetaEstado(
                RojoAlerta, "Te saliste de la ruta planeada",
                "Estás a ${viaje.desvioM ?: desvio.valorMax?.toInt() ?: 0} m de la ruta. La empresa ya recibió la alerta: vuelve a la ruta azul."
            )
            retraso != null -> TarjetaEstado(
                RojoAlerta, "Retraso alto",
                "Vas ${textoRetraso(viaje.retrasoS).lowercase(Locale.ROOT)} respecto al plan. La empresa ya recibió la alerta."
            )
            viaje.nivelRetraso == "aviso" -> TarjetaEstado(
                Ambar, "Vas un poco tarde",
                "${textoRetraso(viaje.retrasoS)} respecto al plan."
            )
        }

        // Transmisión
        if (transmitiendo) {
            EstadoDeTransmision()
        } else {
            TarjetaOrbita(borde = NaranjaMarca) {
                TextoFuerte("La transmisión está detenida", tamano = 16.sp, color = NaranjaMarca)
                TextoSuave(
                    "El viaje sigue en curso, pero la empresa no está recibiendo tu posición.",
                    tamano = 14.sp,
                    modifier = Modifier.padding(top = 2.dp)
                )
            }
            ZonaDeInicio(
                fase = fase,
                titulo = "Reanudar transmisión",
                subtitulo = "Se verifica al conductor y se sigue el mismo recorrido",
                pcNombre = pcNombre,
                acciones = acciones
            )
        }

        // Avance (al llegar, el servidor deja el avance del momento en que entró a 100 m del destino:
        // para el conductor se muestra completo)
        val porcentaje = if (enDestino) 100 else viaje.porcentaje
        val avanceM: Number = if (enDestino) viaje.distanciaM else viaje.avanceM
        TarjetaOrbita(borde = colorEstadoViaje(viaje.estado)) {
            Row(verticalAlignment = Alignment.Bottom) {
                TextoFuerte("$porcentaje %", tamano = 28.sp)
                Spacer(modifier = Modifier.width(10.dp))
                TextoSuave(
                    "${textoKm(avanceM)} de ${textoKm(viaje.distanciaM)}",
                    tamano = 14.sp,
                    modifier = Modifier.padding(bottom = 4.dp)
                )
            }
            BarraAvance(porcentaje / 100f, colorEstadoViaje(viaje.estado))
            Spacer(modifier = Modifier.height(14.dp))
            Row {
                Dato("RECORRIDO REAL", textoKm(viaje.recorridoM), modifier = Modifier.weight(1f))
                Dato("VELOCIDAD", viaje.velocidadKmh?.let { "$it km/h" } ?: "—", modifier = Modifier.weight(1f))
            }
            Spacer(modifier = Modifier.height(10.dp))
            Row {
                Dato(
                    "RETRASO",
                    if (viaje.estado == "en_ruta") textoRetraso(viaje.retrasoS) else "—",
                    modifier = Modifier.weight(1f)
                )
                Dato(
                    if (enDestino) "LLEGÓ" else "LLEGADA ESTIMADA",
                    if (enDestino) horaCorta(viaje.puntos.lastOrNull()?.horaReal) else horaCorta(viaje.llegadaEstimada),
                    modifier = Modifier.weight(1f)
                )
            }
        }

        val proximo = viaje.proximoPunto
        if (proximo != null && !enDestino) {
            TarjetaOrbita(borde = CianMarca) {
                Etiqueta("Próximo punto", color = CianMarca)
                TextoFuerte(proximo.nombre, tamano = 16.sp, modifier = Modifier.padding(top = 2.dp))
                TextoSuave(
                    "Km ${textoKm(proximo.distanciaM).removeSuffix(" km")} · plan ${horaCorta(proximo.horaPlan)}" +
                            (if (viaje.avanceM > 0) " · faltan ${textoKm((proximo.distanciaM - viaje.avanceM).coerceAtLeast(0.0))}" else ""),
                    modifier = Modifier.padding(top = 2.dp)
                )
            }
        }

        TarjetaOrbita {
            Etiqueta("Puntos de control")
            Spacer(modifier = Modifier.height(6.dp))
            viaje.puntos.forEach { FilaPunto(it) }
        }

        TarjetaConductor(
            nombre = if (transmitiendo) EstadoTransmision.conductorNombre ?: viaje.conductor else viaje.conductor,
            cedula = if (transmitiendo) EstadoTransmision.conductorCedula else null
        )

        TarjetaParada(
            etiqueta = "Entregar en",
            direccion = viaje.destinoDireccion,
            persona = viaje.destinatario,
            celular = viaje.destinoCelular,
            nota = null,
            onLlamar = acciones.onLlamar
        )

        // Terminar
        if (enDestino) {
            BotonPrincipal("Finalizar viaje", cargando = finalizando, color = VerdeOk) { acciones.onFinalizar() }
        } else {
            BotonSecundario("Finalizar viaje", modifier = Modifier.padding(top = 4.dp)) { confirmarFinalizar = true }
        }
        if (transmitiendo) {
            Text(
                "Detener la transmisión sin finalizar",
                fontFamily = Manrope,
                fontWeight = FontWeight.SemiBold,
                fontSize = 13.sp,
                color = GrisAzul,
                textAlign = TextAlign.Center,
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(8.dp))
                    .clickable { confirmarDetener = true }
                    .padding(vertical = 10.dp)
            )
        }
        PieServidor()
    }

    if (confirmarFinalizar) {
        Confirmacion(
            titulo = "¿Finalizar el viaje?",
            texto = "Todavía no has llegado al destino (vas en el ${viaje.porcentaje} %). Si lo finalizas, el envío queda " +
                    "terminado y se detiene la transmisión.",
            confirmar = "Finalizar",
            onConfirmar = { confirmarFinalizar = false; acciones.onFinalizar() },
            onCancelar = { confirmarFinalizar = false }
        )
    }
    if (confirmarDetener) {
        Confirmacion(
            titulo = "¿Detener la transmisión?",
            texto = "El viaje sigue en curso, pero la empresa dejará de ver tu posición hasta que la reanudes.",
            confirmar = "Detener",
            onConfirmar = { confirmarDetener = false; acciones.onDetener() },
            onCancelar = { confirmarDetener = false }
        )
    }
}

@Composable
private fun TarjetaEstado(color: Color, titulo: String, texto: String) {
    Card(
        shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(containerColor = color.copy(alpha = 0.16f)),
        border = BorderStroke(1.dp, color.copy(alpha = 0.7f)),
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp)) {
            TextoFuerte(titulo, tamano = 16.sp, color = color)
            TextoSuave(texto, tamano = 14.sp, color = Claro, modifier = Modifier.padding(top = 2.dp))
        }
    }
}

@Composable
private fun EstadoDeTransmision() {
    val error = EstadoTransmision.hayError
    val color = if (error) NaranjaMarca else if (EstadoTransmision.enviados == 0) Ambar else CianMarca
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(Medio.copy(alpha = 0.45f))
            .padding(horizontal = 14.dp, vertical = 10.dp)
    ) {
        Box(
            modifier = Modifier
                .size(10.dp)
                .clip(CircleShape)
                .background(color)
        )
        Spacer(modifier = Modifier.width(10.dp))
        Text(
            text = EstadoTransmision.mensaje,
            fontFamily = Manrope,
            fontWeight = FontWeight.SemiBold,
            fontSize = 13.sp,
            color = Claro,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f)
        )
        if (EstadoTransmision.enviados > 0) {
            TextoSuave("${EstadoTransmision.enviados} · ${EstadoTransmision.ultimoEnvio}", tamano = 12.sp)
        }
    }
}

@Composable
private fun BarraAvance(fraccion: Float, color: Color) {
    Box(
        modifier = Modifier
            .padding(top = 8.dp)
            .fillMaxWidth()
            .height(8.dp)
            .clip(RoundedCornerShape(50))
            .background(Oscuro.copy(alpha = 0.8f))
    ) {
        Box(
            modifier = Modifier
                .fillMaxWidth(fraccion.coerceIn(0f, 1f))
                .fillMaxHeight()
                .clip(RoundedCornerShape(50))
                .background(color)
        )
    }
}

@Composable
private fun FilaPunto(p: PuntoControl) {
    val pasado = p.horaReal != null
    val color = if (pasado) colorNivel(p.nivel) else GrisAzul.copy(alpha = 0.6f)
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 6.dp)
    ) {
        Box(
            modifier = Modifier
                .size(26.dp)
                .clip(CircleShape)
                .background(color)
                .border(2.dp, if (p.tipo == "control") Color.White.copy(alpha = 0.85f) else Oscuro, CircleShape),
            contentAlignment = Alignment.Center
        ) {
            Text(
                when (p.tipo) {
                    "salida" -> "S"
                    "llegada" -> "L"
                    else -> p.orden.toString()
                },
                fontFamily = Manrope,
                fontWeight = FontWeight.ExtraBold,
                fontSize = 11.sp,
                color = Color.White
            )
        }
        Spacer(modifier = Modifier.width(12.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                p.nombre,
                fontFamily = Manrope,
                fontWeight = FontWeight.ExtraBold,
                fontSize = 14.sp,
                color = if (pasado) Claro else Claro.copy(alpha = 0.8f),
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
            TextoSuave(
                "Plan ${horaCorta(p.horaPlan)}" + (if (pasado) " · pasó ${horaCorta(p.horaReal)}" else ""),
                tamano = 12.sp
            )
        }
        if (pasado && p.tipo != "salida") {
            Spacer(modifier = Modifier.width(8.dp))
            Insignia(textoRetraso(p.retrasoS), colorNivel(p.nivel))
        }
    }
}

@Composable
private fun Confirmacion(
    titulo: String,
    texto: String,
    confirmar: String,
    onConfirmar: () -> Unit,
    onCancelar: () -> Unit
) {
    AlertDialog(
        onDismissRequest = onCancelar,
        confirmButton = {
            TextButton(onClick = onConfirmar) {
                Text(confirmar, fontFamily = Manrope, fontWeight = FontWeight.ExtraBold, color = NaranjaMarca)
            }
        },
        dismissButton = {
            TextButton(onClick = onCancelar) {
                Text("Cancelar", fontFamily = Manrope, fontWeight = FontWeight.ExtraBold, color = CianMarca)
            }
        },
        title = { Text(titulo, fontFamily = Manrope, fontWeight = FontWeight.ExtraBold) },
        text = { Text(texto, fontFamily = Manrope, fontWeight = FontWeight.SemiBold, fontSize = 14.sp) },
        containerColor = Oscuro,
        titleContentColor = Claro,
        textContentColor = Claro
    )
}
