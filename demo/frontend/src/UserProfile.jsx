import { useEffect, useState } from "react";

export function UserProfile({ userId }) {
  const [user, setUser] = useState(null);
  useEffect(() => {
    fetch(`/api/users/${userId}`)
      .then(r => r.json())
      .then(setUser)
      .catch(() => setUser(null));
  }, [userId]);
  return <div>{user?.name}</div>;
}
