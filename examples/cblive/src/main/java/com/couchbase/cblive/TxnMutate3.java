package com.couchbase.cblive;

  import com.couchbase.client.core.error.DocumentNotFoundException;
  import com.couchbase.client.java.Bucket;
  import com.couchbase.client.java.Cluster;
  import com.couchbase.client.java.Collection;
  import com.couchbase.client.java.json.JsonArray;
  import com.couchbase.client.java.json.JsonObject;
  import com.couchbase.client.java.query.QueryResult;
  import com.couchbase.transactions.TransactionDurabilityLevel;
  import com.couchbase.transactions.TransactionGetResult;
  import com.couchbase.transactions.Transactions;
  import com.couchbase.transactions.config.TransactionConfigBuilder;
  import com.couchbase.transactions.error.TransactionCommitAmbiguous;
  import com.couchbase.transactions.error.TransactionFailed;
  import com.couchbase.transactions.log.TransactionEvent;

  import java.time.Duration;
  import java.util.UUID;
  import java.util.concurrent.atomic.AtomicReference;


  class TxnMutate3 {
	    
    public static void main(String[] args) {
		var clusterName = "couchbase://172.23.96.189";
		var username = "Administrator";
		var password = "password";
		var bucketName = "travel-sample";

      Cluster cluster = Cluster.connect(clusterName, username, password);
      Bucket bucket = cluster.bucket(bucketName);
      bucket.waitUntilReady(Duration.ofSeconds(30));

      TransactionConfigBuilder config = TransactionConfigBuilder.create()
          .durabilityLevel(TransactionDurabilityLevel.NONE);
      Transactions transactions = Transactions.create(cluster, config);
      
      var route1Id = "route_46586";
      var route2Id = "route_35816";

      // Pre-transaction
      JsonObject preroute1 = getDocById(cluster, bucketName, route1Id);
      JsonObject preroute2 = getDocById(cluster, bucketName, route2Id);
      
      System.out.printf(
          "Before transaction - got %s's details: source airport: %s, destination airport: %s, airline: %s:%s \n",
          route1Id, preroute1.getString("sourceairport"), preroute1.getString("destinationairport"),
          preroute1.getString("airline"), preroute1.getString("airlineid"));
      System.out.printf(
          "Before transaction - got %s's details: source airport: %s, destination airport: %s, airline: %s:%s \n",
          route2Id, preroute2.getString("sourceairport"), preroute2.getString("destinationairport"),
          preroute2.getString("airline"), preroute2.getString("airlineid"));
      
      // In-transaction
      try {
        String swapId = swapAirlineRoute(transactions, cluster, bucketName, route1Id, route2Id);
        if (swapId != null) {
          JsonObject swapRecord = getDocById(cluster, bucketName,swapId);
          System.out.println("After transaction - swap route record: " + swapRecord);
        }
      } catch (RuntimeException err) {
        System.err.println("Transaction failed with: " + err.toString());
      }

      // Post-transaction
      JsonObject postroute1 = getDocById(cluster, bucketName, route1Id);
      JsonObject postroute2 = getDocById(cluster, bucketName, route2Id);
      
      System.out.printf(
          "After transaction - got %s's details: source airport: %s, destination airport: %s, airline: %s:%s \n",
          route1Id, postroute1.getString("sourceairport"), postroute1.getString("destinationairport"),
          postroute1.getString("airline"), postroute1.getString("airlineid"));
      System.out.printf(
          "After transaction - got %s's details: source airport: %s, destination airport: %s, airline: %s:%s \n",
          route2Id, postroute2.getString("sourceairport"), postroute2.getString("destinationairport"),
          postroute2.getString("airline"), postroute2.getString("airlineid"));
      System.exit(0);
    }
    
    private static JsonObject getDocById(Cluster cluster, String bucketName, String docId) {
    	String sqry = "SELECT `" + bucketName + "`.* " + 
    					"FROM `" + bucketName + "` " + 
    					"WHERE META().id = '" + docId + "'";
        QueryResult qr = cluster.query(sqry);
        return qr.rowsAsObject().size()>0? qr.rowsAsObject().get(0): null;
    }

    private static String swapAirlineRoute(Transactions transactions, Cluster cluster, String bucketName, String route1Id,
        String route2Id) {
      AtomicReference<String> swapId = new AtomicReference<>();
      try {
        transactions.run(ctx -> {
        	JsonObject route1Content = getDocById(cluster, bucketName, route1Id);
            JsonObject route2Content = getDocById(cluster, bucketName, route2Id);
            
          System.out.printf("In transaction - got %s's details: %s\n", route1Id,
              route1Content.getString("airlineid"));
          System.out.printf("In transaction - got %s's details: %s\n", route2Id,
              route2Content.getString("airlineid"));

          String airlineid1 = route1Content.getString("airlineid");
          String airline1 = route1Content.getString("airline");
          JsonArray schedule1 = route1Content.getArray("schedule");
          String airline2 = route2Content.getString("airline");
          String airlineid2 = route2Content.getString("airlineid");
          JsonArray schedule2 = route2Content.getArray("schedule");
  
          JsonObject swapRecord = JsonObject.create().put("from_route", route1Id).put("to_route", route2Id)
              .put("from_airline", airline1).put("from_airlineid", airlineid1).put("to_airline", airline2)
              .put("to_airlineid", airlineid2).put("type", "Transactions_History")
              .put("created", new java.util.Date().toString());
          swapId.set(UUID.randomUUID().toString());

          System.out.println("In transaction - creating a record of swap routes with UUID: " + swapId.get());
          ctx.query("INSERT INTO `"+bucketName+"` VALUES ('"+swapId.get()+"', "+swapRecord+")");
          
          System.out.printf("In transaction - changing %s's airline to: %s\n", route1Id,
              route1Content.getString("airlineid"));
          System.out.printf("In transaction - changing %s's airline to: %s\n", route2Id,
              route2Content.getString("airlineid"));

          String updateRoute1 = "UPDATE `"+bucketName+"` " + 
        		  				"SET airline='"+airline2+"', airlineid='"+airlineid2+"', schedule="+schedule2 +
        		  				" WHERE META().id = '"+route1Id+"'";
          ctx.query(updateRoute1);
          String updateRoute2 = "UPDATE `"+bucketName+"` " + 
        		  				"SET airline='"+airline1+"', airlineid='"+airlineid1+"', schedule="+schedule1 +
        		  				" WHERE META().id = '"+route2Id+"'";
          ctx.query(updateRoute2);

          System.out.println("In transaction - about to commit");
          ctx.commit();
        });
      } catch (TransactionCommitAmbiguous err) {
        System.err.println("Transaction " + err.result().transactionId() + " possibly committed:");
        err.result().log().logs().forEach(System.err::println);
      } catch (TransactionFailed err) {
        if (err.getCause() instanceof DocumentNotFoundException) {
          throw new NoRouteFound();
        } else {
          System.err.println("Transaction " + err.result().transactionId() + " did not reach commit:");
          err.result().log().logs().forEach(System.err::println);
        }
      }
      return swapId.get();

    }
  }