# Arranca un servidor gtk4-broadwayd temporal para ejecutar la UI sin abrir ventanas
# en el escritorio. Se detiene automáticamente al salir del script que lo use.
start_broadway() {
  if ! command -v gtk4-broadwayd >/dev/null 2>&1; then
    echo "gtk4-broadwayd no está disponible; se usará el display actual." >&2
    return 1
  fi
  local display=":$(( 40 + RANDOM % 50 ))"
  gtk4-broadwayd "$display" >/dev/null 2>&1 &
  BROADWAY_PID=$!
  trap 'kill "$BROADWAY_PID" 2>/dev/null || true' EXIT
  export BROADWAY_DISPLAY="$display"
  sleep 0.5
}
