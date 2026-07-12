import { gql, useQuery, useMutation } from "@apollo/client";

const GET_USER = gql`
  query GetUser($id: ID!) {
    user(id: $id) {
      name
    }
  }
`;

const CREATE_ORDER = gql`
  mutation CreateOrder($input: OrderInput!) {
    createOrder(input: $input) {
      id
    }
  }
`;

// planted: selects a field the schema does not declare -> orphan_call
const PHANTOM = gql`
  query {
    phantomField {
      id
    }
  }
`;

// alias + fragment spread must not confuse the parser: real field is orders
const ALIASED = gql`
  query {
    latest: orders {
      ...orderBits
    }
  }
`;

export function Gql() {
  const { data } = useQuery(GET_USER);
  const [create] = useMutation(CREATE_ORDER);
  return null;
}
