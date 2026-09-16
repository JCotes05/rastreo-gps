# Sistema de Rastreo GPS — Despliegue Continuo

Repositorio del proyecto de rastreo GPS. Cualquier cambio fusionado a
`main` se despliega **automáticamente** a las 3 instancias EC2 (Valentina,
Jesús, Adrián) mediante GitHub Actions — nadie necesita conectarse por
SSH para actualizar el código en producción.

## Estructura

```
servidor/       → basedatos.py, listener.py, servidor_web.py, ver_registros.py
web/             → index.html (la página, servida como archivo estático por Nginx)
nginx/           → un archivo de configuración POR INSTANCIA (dominios y
                   certificados SSL distintos, no pueden ser un solo archivo)
systemd/         → los .service (Ubuntu y Amazon Linux)
android/         → MainActivity.kt
.github/workflows/desplegar.yml → el flujo de despliegue automático
```

## Cómo funciona el despliegue automático

1. Alguien crea una rama, hace cambios, y abre un Pull Request hacia `main`.
2. Al fusionar (merge) ese Pull Request, `main` recibe un `push`.
3. Eso dispara `.github/workflows/desplegar.yml`, que corre 3 trabajos
   en paralelo (uno por instancia). Cada uno:
   - Copia los archivos actualizados por SCP.
   - Se conecta por SSH y los instala en su lugar real.
   - Reinicia `nginx`, `listener.service` y `servidor-web.service`.

## Ramas

- `main` — la única que despliega automáticamente. Protegida: no se
  sube directo, solo por Pull Request.
- `valentina`, `jesus`, `adrian` — una por integrante, para probar
  cambios sin tocar las instancias reales.

## El archivo `nombre.txt` (importante)

Cada instancia tiene un `nombre.txt` local, al lado de `servidor_web.py`,
que **nunca se sube a GitHub** (ver `.gitignore`). Ahí vive el nombre de
cada integrante, para que la pestaña del navegador lo muestre — sin que
el despliegue automático le sobreescriba el nombre a los 3 por igual.

## Configurar por primera vez (una sola vez, no en cada despliegue)

En cada instancia, antes del primer despliegue automático:

```bash
echo "Valentina" | sudo tee /home/ubuntu/nombre.txt      # instancia de Valentina
echo "Jesús"     | sudo tee /home/ubuntu/nombre.txt      # tu instancia
echo "Adrián"    | sudo tee /home/ec2-user/nombre.txt    # instancia de Adrián

sudo mkdir -p /var/www/rastreo
```

## Secretos necesarios en GitHub (Settings → Secrets and variables → Actions)

| Nombre del secreto | Contenido |
|---|---|
| `VALENTINA_IP` | Elastic IP de Valentina |
| `VALENTINA_SSH_KEY` | Contenido completo de `valenkey.pem` |
| `JESUS_IP` | Tu Elastic IP |
| `JESUS_SSH_KEY` | Contenido completo de `diseno.pem` |
| `ADRIAN_IP` | Elastic IP de Adrián |
| `ADRIAN_SSH_KEY` | Contenido completo de su llave `.pem` |
