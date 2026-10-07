"""Host credential storage. Service tokens stay backend-only; desktop passwords are human-only."""
import keyring


class Credentials:
    service = "ASHAD.Bot.Local"

    def get(self, reference):
        return keyring.get_password(self.service, reference)

    def save(self, reference, value):
        if not value or not value.strip():
            raise ValueError("A nonempty credential is required")
        backend = keyring.get_keyring()
        if backend.priority <= 0 or "plaintext" in type(backend).__name__.lower():
            raise ValueError("A host credential store is required; plaintext fallback is disabled")
        keyring.set_password(self.service, reference, value.strip())
