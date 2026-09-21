from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: tuple[str, ...] = ()


def has_role(principal: Principal, role: str) -> bool:
    return role in principal.roles
