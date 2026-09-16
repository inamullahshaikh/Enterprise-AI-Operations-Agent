from sqlalchemy import select

from relay_core.db.models.identity import User
from relay_core.db.repositories.base import Repository


class UserRepository(Repository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_google_sub(self, google_sub: str) -> User | None:
        stmt = select(User).where(User.google_sub == google_sub)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create(
        self,
        *,
        email: str,
        full_name: str,
        password_hash: str | None = None,
        google_sub: str | None = None,
        auth_provider: str = "password",
        email_verified: bool = False,
    ) -> User:
        user = User(
            email=email,
            full_name=full_name,
            password_hash=password_hash,
            google_sub=google_sub,
            auth_provider=auth_provider,
            email_verified=email_verified,
        )
        self.session.add(user)
        await self.session.flush()
        return user
